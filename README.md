# LiveCheck
Needs: Python3 with discord.py. The uvloop library is optional.

Just `python run.py` from the main directory in the repo and it will generate a config.ini for you to edit in the config folder. Make your changes and restart the bot.

The essential setup also requires a Twitch account with developer access, keys, whatever. The basic rate limit (determined by Twitch) is 30 requests per minute. This means that if you somehow set it up so that you are watching a long list of streams and more than roughly 25 of them are online at once, you may trigger the limiting. The rate limit is internally handled if that happens so that the request does not disappear forever.

If you want to test things for real, try watching Just Chatting. It could be interesting.
