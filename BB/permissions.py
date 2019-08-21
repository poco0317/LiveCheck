import os
import re
import discord

from discord.ext import commands

from BB.conf import Conf


class Perms:
    #EZ Mod check: commands.has_permissions(manage_messages=True)
    #EZ Admin check: commands.has_permissions(manage_server=True)
    
    def is_owner(ctx):
        owner_id = Conf(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))+"/config/config.ini").owner_id
        if ctx.message.author.id != owner_id:
            raise not_owner
        return True
        
    def is_guild_owner(ctx):
        try:
            return Perms.is_owner(ctx)
        except:
            pass
        if ctx.message.author.id != ctx.message.guild.owner.id:
            raise not_server_owner
        return True
    
    def is_guild_superadmin(ctx):
        try:
            return Perms.is_guild_owner(ctx)
        except:
            pass
        perms = ctx.message.channel.permissions_for(ctx.message.author)
        if perms.administrator:
            return True
        raise not_a_superadmin
    
    def is_guild_admin(ctx):
        try:
            return Perms.is_guild_superadmin(ctx)
        except:
            pass
        perms = ctx.message.channel.permissions_for(ctx.message.author)
        if perms.manage_guild:
            return True
        raise not_an_admin
        
    def is_guild_mod(ctx):
        try:
            return Perms.is_guild_admin(ctx)
        except:
            pass
        perms = ctx.message.channel.permissions_for(ctx.message.author)
        if perms.manage_messages:
            return True
        raise not_a_mod

class PermissionException(commands.CommandError):
    def __init__(self, message):
        super().__init__(message)
class not_owner(PermissionException):
    def __init__(self):
        super().__init__("Only the host of the bot may use this command.")
class not_server_owner(PermissionException):
    def __init__(self):
        super().__init__("Only the server owner may use this command.")
class not_a_mod(PermissionException):
    def __init__(self):
        super().__init__("Only a server mod may use this command. Mods have the tag 'Manage Messages' in one of their roles.")
class not_an_admin(PermissionException):
    def __init__(self):
        super().__init__("Only a server admin may use this command. Admins have the tag 'Manage Server' in one of their roles.")
class not_a_superadmin(PermissionException):
    def __init__(self):
        super().__init__("Only a superadmin may use this command. Superadmins have the tag 'Administrator' in one of their roles.")
