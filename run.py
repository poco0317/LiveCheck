import discord
from BB.live import ServerSettings
from BB.permissions import PermissionException
from BB.bot import Barry
from BB.conf import Conf
from discord.ext import commands
import asyncio
import traceback
import datetime
import sys
import re
import os

### if we ever need to use subprocesses on windows in asyncio:
# if os.name == "nt":
#    loop = asyncio.ProactorEventLoop()
#    asyncio.set_event_loop(loop)
###

# uvloop is fast and im all about overengineering solutions OH YEAH BABY
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

conf = Conf(os.path.dirname(os.path.realpath(__file__))+"/config/config.ini")

print("LiveCheck Bot Beginning...")
bot = commands.Bot(command_prefix="^", description="Just checking Twitch for live channels.")

print("Constructing the largest class...")
gotloop = asyncio.get_event_loop()
BarryBot = Barry(bot, gotloop, conf)


@bot.event
async def on_ready():
    print("List of servers:\n- ", end="")
    print("\n- ".join([guild.name for guild in bot.guilds]))
    print("\n"+str(sum([len(guild.text_channels) for guild in bot.guilds]))+" text channels.")
    print(str(sum([len(guild.voice_channels) for guild in bot.guilds]))+" voice channels.")
    print(str(len(set(bot.get_all_members())))+" distinct members.")
    print("\n\nInitialization complete.")
    for guild in bot.guilds:
        if guild.id == conf.log_server_id:
            for channel in guild.channels:
                if channel.id == conf.log_chan_id:
                    BarryBot.logchan = channel
                    break
            break

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild is None:
        return

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    ''' really sad error catch thing '''
    if isinstance(error, discord.ext.commands.errors.CommandNotFound):
        return
    if isinstance(error, PermissionException):
        return await ctx.send(f"```Error\n{error.message}```")
    if isinstance(error, commands.MissingRequiredArgument):
        try:
            return await ctx.send("```Error\nSome argument is missing:\n"+ctx.command.usage+"```")
        except:
            return await ctx.send("```Error\nSome argument is missing, but for some reason wasn't defined explicitly. Good luck. (report to dev)```")
    if isinstance(error, commands.BadArgument):
        return await ctx.send("```Error\n"+str(error)+"```")
    try:
        if isinstance(error.original, discord.Forbidden):
            if error.original.status == 403 and error.original.text == "Missing Permissions":
                return await ctx.send("```Error\nI am missing some type of permission involved in executing this command.```")
            else:
                return await ctx.send("```Error\nThere was a Forbidden error while executing the command. Status: "+str(error.original.status)+" Text:"+error.original.text+"```")
    except:
        pass

    if isinstance(error, discord.ext.commands.CommandError):
        ms = ""
        cause = ""
        try:
            ms = f"`{error.message}`\n"
        except:
            pass
        try:
            cause = f"```{discord.utils.escape_markdown(''.join(traceback.format_tb(error.__cause__.__traceback__)))}```"
        except:
            pass
        await BarryBot.logchan.send(f"There was a possible serious error of type {type(error)}. {ms}{cause}```{discord.utils.escape_markdown(''.join(traceback.format_tb(error.__traceback__)))}```")
    try:
        traceback.print_exc()
    except:
        print("no traceback")

@bot.event
async def on_guild_join(guild):
    await BarryBot.logchan.send("I have joined a new server called "+guild.name+". ID: "+str(guild.id))

@bot.event
async def on_guild_remove(guild):
    await BarryBot.logchan.send("A server I was in called '"+guild.name+"' disappeared. Maybe I got kicked? ID: "+str(guild.id))    


print("That's done; let's try to connect.")
bot.run(BarryBot.THE_SECRET_TOKEN)
