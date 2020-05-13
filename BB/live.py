import os
import shutil
import traceback
import aiohttp
import asyncio
import discord
import configparser
import datetime as dt

from discord.ext import commands

from BB.DB import *
from BB.permissions import *


class MissingResponseField(Exception):
    '''errors'''
    def __init__(self, json_response, field):
        self.json_response = json_response
        self.field = field

class ValidationError(Exception):
    '''errors2'''
    def __init__(self, response):
        # response is a response object and not json
        self.response = response

class LiveCheck(commands.Cog):
    '''
    Where the bulk of the action happens in terms of checking Twitch for live streams.
    Tries its best to be efficient about sending requests to the Twitch API.
    Major bottleneck is that for every live stream, a follower count request is made.
        This issue snowballs quickly.
    Other requests are done in bulk, by chunks of 100 globally.
    The process (loop):
        Get the full list of servers (build a big list of games and names to query)
        Get the list of streamers using these requests:
            gather_byUser - chunks of 100,              1-n requests
        Make a map of games and game ids using these requests:
            get_game_id_by_names - chunks of 100,       1-n requests
            get_game_name_by_ids - chunks of 100,       1-n requests
        Get the list of streams by category
            gather_byGame - run get_game_id_by_names,   2-n requests
        Get the misc info about streamers/blacklisted streamers
            gather_userinfo_by_id - chunks of 100,      1-n requests
        Figure out the streams that went offline and went online
        Delete offline streams
        Push new streams (per stream per server)
            If no game map is set, request the game     1 request for each instance
            Get the follow count of the stream          1 request per stream
        Edit streams that didn't go offline
            If no game map is set, request the game     1 request for each instance
            Get the follow count of the stream          1 request per stream
    This means per loop, there are at least 9 requests.
    For each live streamer, there is 1 more request per server they are visible in.
    '''
    def __init__(self, bot, config):
        self.BarryBot = bot
        self.bot = bot.bot
        self.config = config
        self.loop = bot.loop

        self.sessions = {}

        # generate the bearer token on startup because we dont feel like maintaining it
        # and its not that bad of a thing anyways unless we keep regenerating it every 2 seconds
        self.auth_token = None
        self.aio_session = None

        self.bot.loop.create_task(self.set_aio())
        self.bot.loop.create_task(self.refresh_token())
        self.bot.loop.create_task(self.livecheck_loop())

    async def set_aio(self):
        # "OAuth" required for any token to validate token
        # "Bearer" required for bearer token to authorize our usual requests
        self.aio_session = aiohttp.ClientSession(headers={"Client-ID": self.config.auth_id, "Authorization": f"Bearer {self.auth_token}"})

    async def validate_token(self):
        tmp_session = aiohttp.ClientSession(headers={"Client-ID": self.config.auth_id, "Authorization": f"OAuth {self.auth_token}"})
        try:
            async with tmp_session.get("https://id.twitch.tv/oauth2/validate") as response:
                output = await response.json()
                left = int(output["expires_in"])
                return left > 0
            tmp_session.close()
        except:
            # Probably failed to validate.
            raise ValidationError(response)

    async def refresh_token(self):
        output = {}
        async with self.aio_session.post(f"https://id.twitch.tv/oauth2/token?client_id={self.config.auth_id}&client_secret={self.config.auth_secret}&grant_type=client_credentials") as response:
            output = await response.json()
            self.auth_token = output["access_token"]
        try:
            self.aio_session.close()
        except:
            pass # uhhhh
        await self.set_aio()
        return output["expires_in"]

    async def livecheck_loop(self):
        failures = []
        while True:
            await asyncio.sleep(300)
            try:
                if len(failures) > 0:
                    for failure in failures:
                        await self.BarryBot.logchan.send(failure)
                    failures = []
                if not await self.validate_token():
                    await self.BarryBot.logchan.send("Token failed to validate. It may have expired. Refreshing.")
                    expire_time = await self.refresh_token()
                    await self.BarryBot.logchan.send(f"Token refreshed. It should expire in {expire_time}")
                await self.aggregate_and_refresh_all()
            except MissingResponseField as e:
                failures.append(f"{dt.datetime.utcnow()} Failed due to missing JSON response field\nJSON: {e.json_response} MISSING FIELD: {e.field}")
                try:
                    traceback.print_exc()
                    await self.BarryBot.logchan.send("There was an exception in the stream update loop.")
                except:
                    failures.append(f"{dt.datetime.utcnow()} Failed to send error report to log channel.")
            except ValidationError as e:
                failures.append(f"{dt.datetime.utcnow()} Failed due to Validation Error. Bad URL or Response Parsing\nResponse: {str(e.response)}")
                try:
                    expire_time = await self.refresh_token()
                    failures.append(f"... Successfully refreshed token with expire time {expire_time}")
                except:
                    failures.append("... And then failed to refresh token.")
                try:
                    traceback.print_exc()
                    await self.BarryBot.logchan.send("There was an exception in the stream update loop.")
                except:
                    failures.append(f"{dt.datetime.utcnow()} Failed to send error report to log channel.")
            except Exception as e:
                failures.append(f"{dt.datetime.utcnow()} Failed due to {e}")
                try:
                    traceback.print_exc()
                    await self.BarryBot.logchan.send("There was an exception in the stream update loop.")
                except:
                    failures.append(f"{dt.datetime.utcnow()} Failed to send error report to log channel.")

    async def cleanupStreams(self, guild_id):
        '''delete old messages'''
        sess = self.sessions[guild_id]
        if sess.updating: return False
        sess.updating = True
        messages = sess.brainDB.getTable("messages")
        sess.brainDB.emptyTable("messages")
        sess.created_messages = set()
        channel = None
        try:
            channel = discord.utils.get(self.bot.get_all_channels(), id=int(sess.settings.configuration["channel_id"]))
        except:
            return False
        mm = await channel.send("Searching the past 100 messages to delete...")
        async for msg in channel.history(limit=100, before=mm):
            if msg.author == self.bot.user:
                await msg.delete()
        await mm.edit(content="Done.", delete_after=5)
        sess.updating = False

    async def aggregate_and_refresh_all(self, specific_guild=None):
        '''get all streams for all servers'''
        new_stream_dict, game_map = await self.get_streams_for_all_guilds(specific_guild)
        # new_stream_dict is:
        # a dict mapping guild ids to dicts, mapping streamer names to tuples of (userinfo, streaminfo)
        todo = self.sessions.keys()
        if specific_guild is not None:
            todo = [specific_guild]
        for guild_id in todo:
            try:
                if guild_id not in new_stream_dict: continue
                sess = self.sessions[guild_id]
                if sess.updating: continue
                try:
                    channel = discord.utils.get(self.bot.get_all_channels(), id=int(sess.settings.configuration["channel_id"]))
                except:
                    continue
                sess.updating = True
                old_streams = list(sess.created_messages)
                old_stream_ids = {x[2] for x in sess.created_messages}
                dead_streams = set()
                edit_streams = []
                new_streams = []
                for stream in old_streams.copy():
                    if stream[1] not in new_stream_dict[guild_id]:
                        old_streams.remove(stream)
                        old_stream_ids.remove(stream[2])
                        dead_streams.add(stream)
                    else:
                        edit_streams.append((stream[0], new_stream_dict[guild_id][stream[1]]))
                await self.kill_old_stream(guild_id, dead_streams, channel)
                new_streams.extend(old_streams)
                for stream_name, stream in new_stream_dict[guild_id].items():
                    if stream[1]["user_id"] not in old_stream_ids:
                        new_streams.append(await self.push_new_stream(guild_id, stream, channel, game_map))
                await self.update_old_streams(guild_id, edit_streams, channel, game_map)
                sess.created_messages = set(new_streams)
                sess.update()
                sess.updating = False
            except Exception as e:
                sess.updating = False
                await self.BarryBot.logchan.send(f"Error in updating for guild {guild_id} ```\n{''.join(traceback.format_tb(e.__traceback__))}```")
                traceback.print_exc()
                continue

    async def kill_old_stream(self, guild_id, streams, channel=None):
        '''delete the message for old streams'''
        sess = self.sessions[guild_id]
        if channel is None:
            try:
                channel = discord.utils.get(self.bot.get_all_channels(), id=int(sess.settings.configuration["channel_id"]))
            except:
                return False
        for stream in streams:
            try:
                msg = await channel.fetch_message(stream[0])
                await msg.delete()
            except:
                continue

    async def push_new_stream(self, guild_id, stream, channel=None, game_map=None):
        '''push a new message for a new stream'''
        userinfo = stream[0]
        stream = stream[1]
        sess = self.sessions[guild_id]
        if channel is None:
            try:
                channel = discord.utils.get(self.bot.get_all_channels(), id=int(sess.settings.configuration["channel_id"]))
            except:
                return False
        game_name = "(No Category)"
        if game_map is None:
            try:
                game_name = (await self.get_game_name_by_ids([stream["game_id"]]))[stream["game_id"]]
            except:
                pass
        elif stream["game_id"] != "0":
            try:
                game_name = game_map[stream["game_id"]]
            except:
                game_name = "(Unknown Category)"
        stream_id = stream["user_id"]
        followers = await self.get_followcount_by_id(stream_id)
        e = self.produce_stream_embed(stream, userinfo, game_name, followers)
        msg = await channel.send(embed=e)
        return (str(msg.id), userinfo["login"], stream_id)

    async def update_old_streams(self, guild_id, streams, channel=None, game_map=None):
        '''edit existing embeds for old streams
        each entry in the streams list is a tuple of a message_id and a stream object'''
        sess = self.sessions[guild_id]
        if channel is None:
            try:
                channel = discord.utils.get(self.bot.get_all_channels(), id=int(sess.settings.configuration["channel_id"]))
            except:
                return False
        for duple in streams:
            stream = duple[1][1]
            userinfo = duple[1][0]
            msg_id = duple[0]
            msg = None
            try:
                msg = await channel.fetch_message(msg_id)
            except:
                continue
            game_name = "(No Category)"
            if game_map is None:
                try:
                    game_name = (await self.get_game_name_by_ids([stream["game_id"]]))[stream["game_id"]]
                except:
                    pass
            elif stream["game_id"] != "0":
                try:
                    game_name = game_map[stream["game_id"]]
                except:
                    game_name = "(Unknown Category)"
            stream_id = stream["user_id"]
            followers = await self.get_followcount_by_id(stream_id)
            e = self.produce_stream_embed(stream, userinfo, game_name, followers)
            await msg.edit(embed=e)

    def produce_stream_embed(self, stream, userinfo, game, follows):
        '''return a discord embed based on the info given'''
        title = stream["title"].strip() if "title" in stream else "(blank title)"
        thumb = stream["thumbnail_url"].replace("{width}", "256").replace("{height}", "144")
        e = discord.Embed(title=f"{stream['user_name']} playing {game}", description = f'"{title}"', color=discord.Color.dark_purple(), timestamp=dt.datetime.utcnow(), url=f"https://twitch.tv/{userinfo['login']}")
        e.set_author(name="Live on Twitch:")
        e.set_footer(text="Twitch", icon_url=self.bot.user.avatar_url)
        e.set_image(url=thumb)
        e.set_thumbnail(url=userinfo["profile_image_url"])
        e.add_field(name="Followers", value=follows)
        e.add_field(name="Total Views", value=userinfo["view_count"])
        e.add_field(name="Current Views", value=stream["viewer_count"])
        btype = userinfo["broadcaster_type"]
        if btype == "":
            btype = "Non-Affiliate"
        else:
            btype = btype.capitalize()
        e.add_field(name="Status", value=btype)
        desc = userinfo["description"]
        if desc == "":
            desc = "No description"
        e.add_field(name="Description", value=desc)
        return e

    async def get_streams_for_all_guilds(self, specific_guild=None):
        '''return a dict of all streams for all guilds
        mapping user ids to stream dicts'''
        skipped_guilds = set()
        games = set()
        users = set()
        blacks = set() # blacklisted users
        whites = set() # whitelisted categories for user defined lists
        output = {}
        todo = self.sessions.keys()
        if specific_guild is not None:
            todo = [specific_guild]
        for guild_id in todo:
            sess = self.sessions[guild_id]
            try:
                channel = discord.utils.get(self.bot.get_all_channels(), id=int(sess.settings.configuration["channel_id"]))
            except:
                skipped_guilds.add(guild_id)
                continue
            # doing these feels redundant and actually useless but im going to leave it here
            games |= set(sess.settings.get("Config", "defined_games"))
            users |= set(sess.settings.get("Config", "defined_streams"))
        user_streams = await self.gather_byUser(list(users))
        game_ids = set()
        games_to_resolve = set()
        for stream in user_streams:
            game_ids.add(stream["game_id"])
        game_id_mappings = await self.get_game_id_by_names(list(games)) # a map of names to ids
        game_id_mappings2 = dict((v,k) for k,v in game_id_mappings.items()) # swapped version of that list
        for gameid in game_ids:
            if gameid not in game_id_mappings2:
                games_to_resolve.add(gameid)
        additional_mappings = await self.get_game_name_by_ids(list(games_to_resolve))
        for k,v in additional_mappings.items():
            game_id_mappings2[k] = v
        game_streams = await self.gather_byGame(list(games))
        unique_combo = game_streams + user_streams
        all_streams_by_id = {x["user_id"]:x for x in unique_combo}
        all_stream_ids = {x["user_id"] for x in game_streams} | {x["user_id"] for x in user_streams}
        all_stream_userinfo = await self.gather_userinfo_by_id(list(all_stream_ids))
        # building a big dict of streams from the user info and the given streams
        dict_o_streams = {}
        for stream in all_stream_userinfo:
            # maps a login name to a tuple of (user info, stream info)
            dict_o_streams[stream["login"]] = (stream, all_streams_by_id[stream["id"]])
        # lets get this bread
        for guild_id in todo:
            if guild_id in skipped_guilds: continue
            sess = self.sessions[guild_id]
            blacks = set(sess.settings.get('Config', "blacklisted_streams"))
            whites = set(sess.settings.get('Config', "whitelisted_games"))
            title_contains = set(sess.settings.get('Config', "title_contains"))
            guild_streams = {}
            # by game
            for category in sess.settings.get("Config", "defined_games"):
                if len(whites) > 0 and category not in whites: continue # skip non whitelisted categories if applicable
                for name, stream_tuple in dict_o_streams.items():
                    if stream_tuple[0]["login"] in blacks: continue
                    if name in guild_streams: continue # skip streams already added
                    # skip streams not containing the whitelisted word if applicable
                    if len(title_contains) > 0:
                        allowed = False
                        if "title" in stream_tuple[1]:
                            title = stream_tuple[1]["title"].lower()
                            for phrase in title_contains:
                                if phrase in title:
                                    allowed = True
                        if not allowed: continue
                    # game_id sometimes is empty???
                    if "game_id" in stream_tuple[1]:
                        game_name = game_id_mappings2.get(stream_tuple[1]["game_id"], None)
                        if game_name is None: continue
                        if game_name == category:
                            guild_streams[name] = stream_tuple
            # by name
            for streamer in sess.settings.get("Config", "defined_streams"):
                if streamer in blacks: continue
                if streamer in dict_o_streams:
                    if streamer in guild_streams: continue # skip streams already added
                    # skip streams not containing the whitelisted word if applicable
                    if len(title_contains) > 0:
                        allowed = False
                        if "title" in stream_tuple[1]:
                            title = stream_tuple[1]["title"].lower()
                            for phrase in title_contains:
                                if phrase in title:
                                    allowed = True
                        if not allowed: continue
                    if len(whites) > 0:
                        # game_id sometimes is empty???
                        if "game_id" in dict_o_streams[streamer][1]:
                            # skip non whitelisted categories if applicable
                            game_name = game_id_mappings2.get(dict_o_streams[streamer][1]["game_id"], None)
                            if game_name is not None and game_name.lower() not in whites: continue
                    guild_streams[streamer] = dict_o_streams[streamer]
            output[guild_id] = guild_streams
        # output is:
        # a dict mapping guild ids to dicts, mapping streamer names to tuples of (userinfo, streaminfo)
        return output, game_id_mappings2

    async def wait_for_request_window(self, url):
        '''sometimes we get rate limited. wait for the rate limit window by doing this.'''
        attempt = True
        quit_threshold = 0
        output = {}
        while attempt and quit_threshold < 60:
            async with self.aio_session.get(url) as response:
                output = await response.json()
                if "status" in output:
                    print(f"Had status {output['status']} error.")
                    if output["status"] == 429:
                        print("\tWaiting for 15 seconds.")
                        try:
                            await self.BarryBot.logchan.send(f"Hit rate limit while checking URL: {url}")
                        except:
                            pass
                        await asyncio.sleep(15)
                    else:
                        print(f"\t{output}")
                        quit_threshold += 1
                        await asyncio.sleep(1)
                else:
                    attempt = False
        return output

    def get_json_field(self, json_response, field):
        '''return the json_response data wrapped in error stuff'''
        if field in json_response:
            return json_response[field]
        else:
            raise MissingResponseField(json_response, field)

    async def gather_byGame(self, games):
        '''return the list of streams streaming the list of games given'''
        game_ids = list((await self.get_game_id_by_names(games)).values())
        paginating = True
        stream_list = []
        for i in range(0, len(game_ids), 100):
            paginating = True
            cursor = ""
            while paginating:
                this = f"https://api.twitch.tv/helix/streams?{'&'.join([f'game_id={x}' for x in game_ids[i:i+100]])}&first=100{cursor}"
                json_response = await self.wait_for_request_window(this)
                if len(self.get_json_field(json_response, "data")) != 100:
                    paginating = False
                else:
                    cursor = f'&after={self.get_json_field(json_response, "pagination")["cursor"]}'
                stream_list.extend(self.get_json_field(json_response, "data"))
        return stream_list

    async def gather_byUser(self, users):
        '''return the list of streams by user, if the user is live'''
        paginating = True
        stream_list = []
        for i in range(0, len(users), 100):
            paginating = True
            cursor = ""
            while paginating:
                this = f"https://api.twitch.tv/helix/streams?{'&'.join([f'user_login={x}' for x in users[i:i+100]])}&first=100{cursor}"
                json_response = await self.wait_for_request_window(this)
                if len(self.get_json_field(json_response, "data")) != 100:
                    paginating = False
                else:
                    cursor = f'&after={self.get_json_field(json_response, "pagination")["cursor"]}'
                stream_list.extend(self.get_json_field(json_response, "data"))
        return stream_list

    async def gather_userinfo_by_id(self, users):
        '''return the list of users by id, for extra info'''
        stream_list = []
        for i in range(0, len(users), 100):
            this = f"https://api.twitch.tv/helix/users?{'&'.join([f'id={x}' for x in users[i:i+100]])}"
            json_response = await self.wait_for_request_window(this)
            stream_list.extend(self.get_json_field(json_response, "data"))
        return stream_list
    
    async def get_followcount_by_id(self, user_id):
        '''return the number of followers for a user id'''
        this = f"https://api.twitch.tv/helix/users/follows?to_id={user_id}"
        json_response = await self.wait_for_request_window(this)
        return self.get_json_field(json_response, "total")

    async def get_game_id_by_names(self, game_names):
        '''
        Use new twitch api to get a game id by game name
        game_names needs to be a list of strings
        returns a dict mapping those names to ids
        '''
        game_ids = {}
        for i in range(0, len(game_names), 100):
            this = f"https://api.twitch.tv/helix/games?{'&'.join([f'name={x}' for x in game_names[i:i+100]])}"
            json_response = await self.wait_for_request_window(this)
            for game in self.get_json_field(json_response, "data"):
                game_ids[game["name"]] = game["id"]
        return game_ids

    async def get_game_name_by_ids(self, game_ids):
        '''
        Use new twitch api to get a game name by game id
        game_ids needs to be a list of strings
        returns a dict mapping those ids to names
        '''
        game_names = {}
        for i in range(0, len(game_ids), 100):
            this = f"https://api.twitch.tv/helix/games?{'&'.join([f'id={x}' for x in game_ids[i:i+100]])}"
            json_response = await self.wait_for_request_window(this)
            for game in self.get_json_field(json_response, "data"):
                game_names[game["id"]] = game["name"]
        return game_names

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            self.sessions[guild.id] = LiveBrain(guild.id, self.config)
            self.sessions[guild.id].settings.verify()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if guild.id not in self.sessions:
            self.sessions[guild.id] = LiveBrain(guild.id, self.config)
            self.sessions[guild.id].settings.verify()

    @commands.command()
    @commands.check(Perms.is_owner)
    async def refreshtoken(self, ctx):
        '''- Force the OAuth token to refresh'''
        try:
            expire_time = await self.refresh_token()
            await ctx.send(f"Successfully refreshed: Expires in {expire_time}")
        except:
            traceback.print_exc()
            await ctx.send("Failed to refresh.")

    @commands.command()
    @commands.check(Perms.is_owner)
    async def globalupdate(self, ctx):
        '''- Force update every server for the bot'''
        await self.aggregate_and_refresh_all()
        print("Finished update of all guilds.")
        await ctx.send("Finished update of all guilds.")

    @commands.command()
    @commands.check(Perms.is_owner)
    async def globalerase(self, ctx):
        '''- Force erase stream messages in every server for the bot'''
        for guild in self.sessions:
            await self.cleanupStreams(guild)
            print(f"Finished deletion of guild {guild}")
        await ctx.send("Finished deleting all messages.")

    @commands.command(aliases=["blacklist"])
    @commands.check(Perms.is_guild_mod)
    async def ignore(self, ctx, *streamers):
        '''- Ignore a specified streamer or remove them from the ignored list.'''
        sess = self.sessions[ctx.guild.id]
        if len(streamers) == 0:
            ignored_users = sess.settings.get("Config", "blacklisted_streams")
            if len(ignored_users) == 0:
                return await ctx.send("There are no ignored streams.")
            else:
                try:
                    return await ctx.send(f"These are the ignored streams ({len(ignored_users)} of them):\n```\n"+", ".join(ignored_users)+"```")
                except:
                    return await ctx.send(f"It seems you ignored so many users, the message was too big. There are {len(ignored_users)} users in the list. (contact bot dev for help)")
        removed = set()
        added = set()
        streamers = set([x.lower() for x in streamers])
        for streamer in sorted(streamers):
            if not sess.toggleBlacklist(streamer):
                removed.add(streamer)
            else:
                added.add(streamer)
        finalout = ""
        if len(removed) > 0:
            finalout += f"Un-ignored {len(removed)} streamers:\n```\n" + ", ".join(sorted(removed)) + "```"
        if len(added) > 0:
            finalout += f"\nIgnored {len(added)} streamers:\n```\n" + ", ".join(sorted(added)) + "```"
        return await ctx.send(finalout)

    @commands.command(aliases=["gamelist"])
    @commands.check(Perms.is_guild_mod)
    async def whitelist(self, ctx, *games):
        '''- Set a whitelist of games if watching several streamers.'''
        sess = self.sessions[ctx.guild.id]
        if len(games) == 0:
            whitelisted_games = sess.settings.get("Config", "whitelisted_games")
            if len(whitelisted_games) == 0:
                return await ctx.send("There are no whitelisted games. Any game may show up if a streamer is watched.")
            else:
                try:
                    return await ctx.send(f"These are the whitelisted categories ({len(whitelisted_games)} of them):\n```\n"+", ".join(whitelisted_games)+"```")
                except:
                    return await ctx.send(f"It seems you whitelisted so many games, the message was too big. There are {len(whitelisted_games)} categories in the list. (contact bot dev for help)")
        removed = set()
        added = set()
        games = set([x.lower() for x in games])
        for game in sorted(games):
            if not sess.toggleWhitelist(game):
                removed.add(game)
            else:
                added.add(game)
        finalout = ""
        if len(removed) > 0:
            finalout += f"Un-whitelisted {len(removed)} games:\n```\n" + ", ".join(sorted(removed)) + "```"
        if len(added) > 0:
            finalout += f"\nWhitelisted {len(added)} games:\n```\n" + ", ".join(sorted(added)) + "```"
        return await ctx.send(finalout)

    @commands.command(aliases=["requiregame"])
    @commands.check(Perms.is_guild_mod)
    async def require(self, ctx, *phrases):
        '''- Set a list of phrases all streams must contain. Stream may contain ANY phrase.'''
        sess = self.sessions[ctx.guild.id]
        if len(phrases) == 0:
            title_contains = sess.settings.get("Config", "title_contains")
            if len(title_contains) == 0:
                return await ctx.send("There are no required phrases. Any stream may show up if other conditions are met.")
            else:
                try:
                    return await ctx.send(f"These are the possible required phrases. Streams must contain any phrase ({len(title_contains)} phrases):\n```\n"+"\n".join(title_contains)+"```")
                except:
                    return await ctx.send(f"It seems you added so many required phrases, the message was too big. There are {len(title_contains)} phrases. (contact bot dev for help)")
        removed = set()
        added = set()
        phrases = set([x.lower() for x in phrases])
        for phrase in sorted(phrases):
            if not sess.toggleRequiredPhrase(phrase):
                removed.add(phrase)
            else:
                added.add(phrase)
        finalout = ""
        if len(removed) > 0:
            finalout += f"Removed {len(removed)} phrases:\n```\n" + "\n".join(sorted(removed)) + "```"
        if len(added) > 0:
            finalout += f"\nAdded {len(added)} phrases:\n```\n" + "\n".join(sorted(added)) + "```"
        return await ctx.send(finalout)


    @commands.command(aliases=["cat", "category", "watch"])
    @commands.check(Perms.is_guild_mod)
    async def game(self, ctx, *, game_name : str = "give me the list"):
        '''- Add a specified game to the list or remove it. Name must be exact.'''
        sess = self.sessions[ctx.guild.id]
        if game_name == "give me the list":
            games = sess.settings.get("Config", "defined_games")
            if len(games) == 0:
                return await ctx.send("There are no watched categories.")
            else:
                try:
                    return await ctx.send(f"These are the watched categories ({len(games)} of them):\n```\n"+", ".join(games)+"```")
                except:
                    return await ctx.send(f"It seems you watch so many categories, the message was too big. There are {len(games)} categories in the list. (contact bot dev for help)")
        if sess.toggleGame(game_name):
            return await ctx.send(f"{game_name} is being watched.")
        else:
            return await ctx.send(f"{game_name} is no longer being watched.")
    
    @commands.command(aliases=["addstream", "streamer", "user"])
    @commands.check(Perms.is_guild_mod)
    async def stream(self, ctx, streamer : str = "give me the list"):
        '''- Add a specified streamer to the list or remove them.'''
        sess = self.sessions[ctx.guild.id]
        streamer = streamer.lower()
        if streamer == "give me the list":
            streams = sess.settings.get("Config", "defined_streams")
            if len(streams) == 0:
                return await ctx.send("There are no watched streams.")
            else:
                try:
                    return await ctx.send(f"These are the watched streams ({len(streams)} of them):\n```\n"+", ".join(sorted(streams))+"```")
                except:
                    return await ctx.send(f"It seems you watch so many streams, the message was too big. There are {len(streams)} streamers in the list. (contact bot dev for help)")
        if not sess.toggleStreamer(streamer):
            return await ctx.send(f"{streamer} is no longer watched.")
        else:
            return await ctx.send(f"{streamer}'s streams will be watched.")

    @commands.command(name="resetstreams", aliases=["resetusers"])
    @commands.check(Perms.is_guild_mod)
    async def reset_streams(self, ctx):
        '''- Reset the list of streamers watched to empty it.'''
        sess = self.sessions[ctx.guild.id]
        sess.settings.modify("Config", "defined_streams", "")
        return await ctx.send("I have reset the list of streamers watched.")

    @commands.command(name="streamsfromlist", aliases=["bulkusers", "bulkstreams"])
    @commands.check(Perms.is_guild_mod)
    async def bulk_add_streams(self, ctx, *streamers):
        '''- Replace the current list of streamers by another list of streamers.'''
        sess = self.sessions[ctx.guild.id]
        the_streamers = "^^".join(streamers).lower()
        sess.settings.modify("Config", "defined_streams", the_streamers)
        return await ctx.send(f"I have reset the watched stream list to {len(streamers)} streamers.")

    @commands.command(name="channel", aliases=["chan", "setchan"])
    @commands.check(Perms.is_guild_mod)
    async def _channel(self, ctx, chan : discord.TextChannel = None):
        '''- Set the output channel for the livestream messages.'''
        sess = self.sessions[ctx.guild.id]
        if chan is None:
            channel = None
            try:
                channel = await commands.TextChannelConverter().convert(ctx, sess.settings.configuration["channel_id"])
            except:
                pass
            if channel is None:
                return await ctx.send("There is no set output channel.")
            else:
                return await ctx.send(f"The current output channel is {channel.mention}")
        sess.setChannel(chan.id)
        return await ctx.send(f"The current output channel is now {chan.mention}")

    @commands.command()
    @commands.check(Perms.is_guild_mod)
    async def refresh(self, ctx):
        '''- Force a refresh of the live check. (Destructive)
        This will delete all messages and create new ones.'''
        sess = self.sessions[ctx.guild.id]
        channel = None
        try:
            channel = await commands.TextChannelConverter().convert(ctx, sess.settings.configuration["channel_id"])
        except:
            pass
        if channel is None:
            return await ctx.send("There is no set output channel.")
        else:
            locked = await self.cleanupStreams(ctx.guild.id)
            if locked == False:
                return await ctx.send("Livestream updating is currently locked due to an ongoing refresh.")
            await self.aggregate_and_refresh_all(ctx.guild.id)
            return await ctx.send(f"Livestreams have been refreshed in {channel.mention}")

    @commands.command()
    @commands.check(Perms.is_guild_mod)
    async def update(self, ctx):
        '''- Force an update of the live check. 
        This will create new, edit existing, and delete old messages.'''
        sess = self.sessions[ctx.guild.id]
        channel = None
        try:
            channel = await commands.TextChannelConverter().convert(ctx, sess.settings.configuration["channel_id"])
        except:
            pass
        if channel is None:
            return await ctx.send("There is no set output channel.")
        else:
            locked = await self.aggregate_and_refresh_all(ctx.guild.id)
            if locked == False:
                return await ctx.send("Livestream updating is currently locked due to an ongoing refresh.")
            return await ctx.send(f"Livestreams have been updated in {channel.mention}")

class LiveBrain:
    ''' like a brain for each server, a db instance, whatever you want (also holds a ServerSettings instance)'''
    def __init__(self, serverID, config):
        self.serverID = str(serverID)
        self.brainDB = GeneralDB("live_"+self.serverID)
        self.verifyTables()
        
        # this holds a dict called configuration which holds 5 keys
        # "defined_streams", "defined_games", "blacklisted_streams", "whitelisted_games", "title_contains", "channel_id"
        # the first 5 are strings in the form of a list while the last is a single id
        self.settings = ServerSettings(serverID, config)

        self.created_messages = set()
        self.updating = False

        self.compile()

    def compile(self):
        ''' set up the main stuff.'''
        for row in self.brainDB.getTable("messages"):
            self.created_messages.add(row)

    def verifyTables(self):
        '''make sure all the tables exist.'''
        if not self.brainDB.verifyTableExists("messages"):
            self.brainDB.createTable("messages", ["message_id text", "streamer text", "user_id text"])

    def getStreamIDsFromMessages(self):
        '''return the list of user_ids from the message set'''
        output = []
        for x in self.created_messages:
            output.append(x[2])
        return output

    def update(self):
        '''update the db to match the set of messages'''
        self.brainDB.emptyTable("messages")
        if len(self.created_messages) > 0:
            print(f"Saving DB for server {self.serverID}\n\t{' '.join([x[1] for x in self.created_messages])}")
            self.brainDB.addRows("messages", list(self.created_messages))

    def toggleBlacklist(self, streamer):
        '''add or remove a user from the blacklist'''
        return self.__toggleConfigThing("blacklisted_streams", streamer)

    def toggleWhitelist(self, game):
        '''add or remove a game from the whitelist'''
        return self.__toggleConfigThing("whitelisted_games", game)

    def toggleGame(self, game_name):
        '''add or remove a game from the list'''
        return self.__toggleConfigThing("defined_games", game_name)

    def toggleStreamer(self, streamer):
        '''add or remove a streamer from the list'''
        return self.__toggleConfigThing("defined_streams", streamer)

    def toggleRequiredPhrase(self, phrase):
        '''add or remove a phrase from the whitelist'''
        return self.__toggleConfigThing("title_contains", phrase)

    def setChannel(self, chan_id):
        '''set the channel id'''
        self.settings.modify("Config", "channel_id", str(chan_id))
        return True

    def __toggleConfigThing(self, place, item):
        '''something to shorten the above statements'''
        things = self.settings.get("Config", place)
        if item in things:
            things.remove(item)
            self.settings.modify("Config", place, "^^".join(things))
            return False
        things.append(item)
        self.settings.modify("Config", place, "^^".join(things))
        return True



class ServerSettings:
    # this is the object which describes each servers settings
    # it parses a .ini file given by the server id and so forth
    # the string list thing relies on splitting stuff by "^^" which IS SUCH A BAD IDEA OH MY GOD
    
    def __init__(self, serverID, config):
        self.serverID = str(serverID)
        self.config_filepath = os.path.dirname(config.options)+"/settings/"+str(serverID)+".ini"
        self.config = configparser.ConfigParser(interpolation=None)
        if not self.config.read(self.config_filepath, encoding='utf-8'):
            try:
                os.makedirs(os.path.dirname(self.config_filepath), exist_ok=True)
                shutil.copy(os.path.dirname(config.options)+"/example_server.ini", self.config_filepath)
            except:
                traceback.print_exc()
                print("failure")
    
        self.config.read(self.config_filepath, encoding='utf-8')

        try:
            self.configuration = self.config["Config"]         # Server Config
        except:
            print("I had to verify a server's settings: "+self.serverID)
            self.verify()

    def verify(self):
        # to check back to the example ini and copy over missing settings in case of an update

        # this function should probably be run every time something is modified and on every bot restart per server
        # as well as on a server join

        example_config_path = os.path.dirname(os.path.dirname(self.config_filepath))+"/example_server.ini"
        configurerer = configparser.ConfigParser(interpolation=None)

        configurerer.read(example_config_path, encoding='utf-8')

        changes_made = 0

        try:
            for key, value in configurerer["Config"].items():
                if key not in self.config["Config"]:
                    self.config["Config"][key] = value
                    changes_made += 1
        except:
            self.config["Config"] = configurerer["Config"]
            print("Verify error: Missing config setting could not be defaulted, all server config settings reset.")
            changes_made += len(configurerer["Config"])

        try:
            self.configuration
        except:
            self.configuration = configurerer["Config"]
            self.config["Config"] = configurerer["Config"]
            print("Verify error: Config does not exist on this server. Reset to default.")
            changes_made += len(self.configuration)

        if len(self.configuration) != len(configurerer["Config"]):
            for key in configurerer["Config"]:
                if key not in self.configuration:
                    self.configuration[key] = configurerer["Config"][key]
                    print("Set default config for missing: "+key)
                    changes_made += 1
            for key in self.configuration:
                if key not in configurerer["Config"]:
                    del self.configuration[key]
                    print("Deleted deprecated config setting: "+key)
                    changes_made += 1


        with open(self.config_filepath, "w", encoding="utf-8") as file:
            self.config.write(file)
        return changes_made

    def sanity_check(self, guild):
        '''Check all specific settings which reference IDs to make sure they still point to something and also to make sure all settings which should be IDs are IDs'''
        self.sanity_check_individual("Config", "defined_streams", guild)
        self.sanity_check_individual("Config", "defined_games", guild)
        self.sanity_check_individual("Config", "blacklisted_streams", guild)
        self.sanity_check_individual("Config", "whitelisted_games", guild)
        self.sanity_check_individual("Config", "title_contains", guild)
        self.sanity_check_individual("Config", "channel_id", guild)

    def sanity_check_individual(self, section, name, guild):
        '''Check an individual setting, even if it is a list, for IDs that don't work'''
        try:
            if self.config[section][name] == "0":
                return
            potential_list = self.config[section][name].split("^^")
            length = len(potential_list)
            for id in potential_list:
                if self.isNone(id, guild):
                    if length > 1:
                        self.remove(section, name, id)
                    else:
                        self.modify(section, name, "0")
        except:
            traceback.print_exc()
            pass

    def isNone(self, givenID, guild):
        '''Check to see if a given ID returns a none type
        returns true if the ID returns all None'''
        ischannel = guild.get_channel(int(givenID))
        isuser = guild.get_member(int(givenID))
        isrole = guild.get_role(int(givenID))
        return ischannel is None and isuser is None and isrole is None

    def validateID(self, chanID, guild):
        '''Check a given channel ID in a server to see if it is legitimate'''
        return int(chanID) in [chan.id for chan in guild.channels]

    def validateIDType(self, chanID, guild, chantype):
        '''Check a given channel ID in a server against a type'''
        if chantype == 'text':
            return int(chanID) in [chan.id for chan in guild.text_channels]
        elif chantype == 'voice':
            return int(chanID) in [chan.id for chan in guild.voice_channels]
        elif chantype == 'category':
            return int(chanID) in [chan.id for chan in guild.categories]
        return False

    def get_default(self, section, name):
        '''Find the default value for a setting'''
        configurerer = configparser.ConfigParser(interpolation=None)
        example_config_path = os.path.dirname(os.path.dirname(self.config_filepath))+"/example_server.ini"
        configurerer.read(example_config_path, encoding="utf-8")
        return configurerer[section][name]

    def num_to_bool(self, section, name, truefalse="on off"):
        '''Return a conversion of 1 or 0 to True or False, basically.
        By default, it converts all cases of 0 to off and 1 to on.
        Supplying a different string like "true false" will make it return true for 1 and false for 0.
        This will return None if something goes wrong.'''
        cases = truefalse.split()
        try:
            if self.config[section][name] == "1":
                return cases[0]
            elif self.config[section][name] == "0":
                return cases[1]
            else:
                return None
        except:
            print("There was an error converting number to boolean cases.")
            return None

    def input_to_bool(self, input, truefalse="on off"):
        '''Essentially the same as num_to_bool except it must be given an input and it determines whether it is true or false from that'''
        cases = truefalse.split()
        try:
            if input == "0" or input == 0 or input == "false" or input == "False" or input == 'no' or input == "No":
                return cases[1]
            elif input == '1' or input == 1 or input == "true" or input == "True" or input == "yes" or input == "Yes":
                return cases[0]
            else:
                return None
        except:
            print("There was an error converting input to boolean cases.")
            return None


    def add(self, section, name, value):
        '''Add a setting to a section in the server setting ini
        Section is the [section]
        name is the name of the setting
        value is what to set the setting to
        Returns false if an error occurs'''
        try:
            self.config[section][name] = value
            with open(self.config_filepath, "w", encoding="utf-8") as file:
                self.config.write(file)
            return True
        except:
            return False

    def remove(self, section, name, value=None):
        '''Remove a setting to a section in the server setting ini
        ... same as add but reversed and doesnt need a value
        If a value is given, it removes it from the list (assuming it should be a list)'''
        if value: #this is for removing an element from a list
            try:
                if len(self.config[section][name].split("^^")) == 1:
                    del self.config[section][name]
                    with open(self.config_filepath, "w", encoding="utf-8") as file:
                        self.config.write(file)
                    return True
                tmpSet = set(self.config[section][name].split("^^"))
                tmpSet.remove(value)
                self.config[section][name] = "^^".join(tmpSet)
                with open(self.config_filepath, "w", encoding="utf-8") as file:
                    self.config.write(file)
                return True
            except:
                return False
        try:
            del self.config[section][name]
            with open(self.config_filepath, "w", encoding="utf-8") as file:
                self.config.write(file)
            return True
        except:
            return False

    def modify(self, section, name, value):
        ''' change a setting
        if its a setting that holds a list, make sure its the right kind of value'''
        try:
            checks = set()
            if name in checks:
                to_remove = []
                for x in value.split("^^"):
                    try:
                        int(x)
                    except:
                        to_remove.append(x)
                valuelist = value.split("^^")
                for x in to_remove:
                    valuelist.remove(x)
                value = "^^".join(valuelist)
            self.config[section][name] = value
            with open(self.config_filepath, "w", encoding="utf-8") as file:
                self.config.write(file)
            return True
        except:
            traceback.print_exc()
            return False

    def toggle(self, section, name):
        '''Basically modify, except toggles a 1 to a 0 and a 0 to a 1
        Returns what we toggled to unless it didnt work'''
        try:
            if self.config[section][name] == '0':
                self.config[section][name] = "1"
                with open(self.config_filepath, "w", encoding="utf-8") as file:
                    self.config.write(file)
                return 1
            elif self.config[section][name] == "1":
                self.config[section][name] = "0"
                with open(self.config_filepath, "w", encoding="utf-8") as file:
                    self.config.write(file)
                return 0
            return None
        except:
            return None

    def get(self, section, name):
        '''return the right thing for the right reasons'''
        try:
            output = self.config[section][name].split("^^")
            realout = [x for x in output if x != '']
            return realout
        except:
            traceback.print_exc()
            return None