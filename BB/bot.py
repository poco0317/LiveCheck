import os
import re
import asyncio
import aiohttp
import random
import discord
import traceback
import unicodedata

from discord.ext import commands

#from BB.file import class
from BB.conf import Conf
from BB.permissions import *
from BB.live import LiveCheck


class Barry(discord.Client):

    def __init__(self, bot, loop, conf):
        self.config = conf
        self.THE_SECRET_TOKEN = self.config.THE_TOKEN
        self.loop = loop
        self.bot = bot

        self.settings = {}

        self.bot.add_cog(Main(self))
        self.bot.add_cog(LiveCheck(self, self.config))

        self.paginators = set()
        
        self.logchan = None

        self.blacklist = set()
        
        super().__init__()

    async def delete_later(self, message, time=15):
        self.loop.create_task(self._actually_delete_later(message, time))

    async def _actually_delete_later(self, message, time=15):
        await asyncio.sleep(time)
        try:
            await message.delete()
        except:
            pass

class Main(commands.Cog):
    def __init__(self, bot):
        self.BarryBot = bot
        self.bot = bot.bot
    
    @commands.command(hidden=True)
    async def report(self, ctx, *, words):
        ''' Report an issue to the developers '''
        await self.BarryBot.logchan.send("REPORT - "+ctx.author.name+" in "+ctx.guild.name+": "+words)
        await ctx.send("Report sent.")

    @commands.command(hidden=True, aliases=["shtudown", "sd", "shtdon", "shutdwon"])
    @commands.check(Perms.is_owner)
    async def shutdown(self, ctx):
        await ctx.send("Shutting down. I will not restart until manually run again.")
        await self.BarryBot.logout()
        await self.bot.logout()

    @commands.command(hidden=True)
    @commands.check(Perms.is_owner)
    async def get_globalinfo(self, ctx):
        ''' Print basic visible info about every channel in every server to the terminal '''
        for guild in self.bot.guilds:
            print(f"Guild: {guild}")
            print(f"Owner: {guild.owner}")
            for chan in guild.text_channels:
                print(f"\tText Channel: {chan} ID: {chan.id} Topic: {chan.topic}")
            for chan in guild.voice_channels:
                print(f"\tVoice Channel: {chan} ID: {chan.id}")
            for chan in guild.categories:
                print(f"\tCategory: {chan} ID: {chan.id}")

    @commands.command(hidden=True)
    @commands.check(Perms.is_guild_superadmin)
    async def judgey(self, ctx, first : discord.Member, second : discord.Member):
        ''' Makes a new channel using the name of the 2 people mentioned.
        Doesn't require mentioning as long as the users exist in the server.
        This is a hidden command for personal use. '''
        overwrites = {
            ctx.guild.default_role : discord.PermissionOverwrite(read_messages=False),
            first : discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True),
            second : discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True)
        }
        await ctx.guild.create_text_channel(f"{first.name}-{second.name}", overwrites=overwrites)
