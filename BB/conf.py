import os
import shutil
import traceback
import configparser


class Conf:
    def __init__(self, conf):
        self.options = conf
        self.config = configparser.ConfigParser(interpolation=None)
        
        if not self.config.read(conf, encoding='utf-8'):
            print("I had to remake the config file from default. Please check the config and restart once the proper settings have been changed.")
            print("The config should exist here: " +self.options)
            try:
                shutil.copy(os.path.dirname(self.options)+"/example_config.ini", self.options)
            except:
                traceback.print_exc()
                print("Well... Somehow the example I was copying from is also gone. You're in a bad spot.")
            os._exit(1)
            
        self.config.read(conf, encoding='utf-8')
        
        self.THE_TOKEN = self.config.get("Login", "Token", fallback=Fallbacks.token)
        self.owner_id = int(self.config.get("Permissions", "OwnerID", fallback=Fallbacks.ownerID))
        self.auth_id = self.config.get("Twitch", "Auth_ID", fallback=Fallbacks.auth_id)
        self.auth_secret = self.config.get("Twitch", "SECRET", fallback=Fallbacks.auth_secret)
        self.log_server_id = int(self.config.get("Logging", "ServerID", fallback=Fallbacks.log_server_id))
        self.log_chan_id = int(self.config.get("Logging", "ChannelID", fallback=Fallbacks.log_chan_id))

    def update(self):
        '''write stuff to the file again'''
        with open(self.options, "w", encoding="utf-8"):
            self.config.write(file)

# these will only get used if the user leaves the config.ini existant but really messes something up... everything breaks if they get used.
class Fallbacks:
    token = "0"
    ownerID = 0
    auth_id = "no"
    auth_secret = "no"
    log_server_id = 0
    log_chan_id = 0
