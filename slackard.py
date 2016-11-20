#!/usr/bin/env python

from __future__ import print_function

from glob import glob
import functools
import importlib
import os.path
import re
import slacker
import sys
import time
import yaml


class SlackardFatalError(Exception):
    pass


class SlackardNonFatalError(Exception):
    pass


class Config(object):
    config = {}

    def __init__(self, file_):
        self.file = file_
        f = open(file_, 'r')
        y = yaml.load(f)
        f.close()
        self.__dict__.update(y)


class Slackard(object):

    subscribers = []
    commands = []
    firehoses = []

    def __init__(self, config_file):
        self.config = Config(config_file)
        self.apikey = self.config.slackard['apikey']
        self.botname = self.config.slackard['botname']
        self.botnick = self.config.slackard['botnick']
        self.channels = self.config.slackard['channel']
        # multi-channel support
        #   self.channels is name, ID pairs, populated in _init_connection
        if ',' in self.channels:
            self.channels = (x.strip() for x in self.channels.split(","))
        else:
            self.channels = (self.channels,)
        self.channels = dict.fromkeys(self.channels)
        self.chan_ids = {}
        self.plugins = self.config.slackard['plugins']
        try:
            self.boticon = self.config.slackard['boticon']
        except:
            self.boticon = None
        try:
            self.botemoji = ':{0}:'.format(self.config.slackard['botemoji'])
        except:
            self.botemoji = None

    def __str__(self):
        return 'I am a Slackard!'

    def _import_plugins(self):
        self._set_import_path()
        plugin_prefix = os.path.split(self.plugins)[-1]

        # Import the plugins submodule (however named) and set the
        # bot object in it to self
        importlib.import_module(plugin_prefix)
        sys.modules[plugin_prefix].bot = self

        for plugin in glob('{}/[!_]*.py'.format(self._get_plugin_path())):
            module = '.'.join((plugin_prefix, os.path.split(plugin)[-1][:-3]))
            try:
                importlib.import_module(module)
            except Exception as e:
                print('Failed to import {0}: {1}'.format(module, e))

    def _get_plugin_path(self):
        """Resolve plugin path"""
        path = self.plugins
        cf = self.config.file
        if path[0] != '/':
            path = os.path.join(os.path.dirname(os.path.realpath(cf)), path)
        return path

    def _set_import_path(self):
        """add plugin path to path"""
        path = self._get_plugin_path()
        # Use the parent directory of plugin path
        path = os.path.dirname(path)
        if path not in sys.path:
            sys.path = [path] + sys.path

    def _init_connection(self):
        """Set up connection and populate channel IDs"""
        self.slack = slacker.Slacker(self.apikey)
        try:
            r = self.slack.channels.list()
        except slacker.Error as e:
            if e.message == 'invalid_auth':
                raise SlackardFatalError('Invalid API key')
            raise
        except Exception as e:
            raise SlackardNonFatalError(e.message)

        c_map = {c['name']: c['id'] for c in r.body['channels']}
        self.chan_ids = {}
        for x in self.channels:
            self.channels[x] = c_map[x]
            self.chan_ids[self.channels[x]] = x

    def _fetch_messages_since(self, oldest=None):
        """Fetch messages from all configured channels"""
        all_messages = []
        for channel in self.channels.values():
            h = self.slack.channels.history(channel, oldest=oldest)
            assert(h.successful)
            messages = h.body['messages']
            # insert channel and chan_id in message attributes
            for x in range(len(messages)):
                messages[x]['chan_id'] = channel
                messages[x]['channel'] = self.chan_ids[channel]
            messages.reverse()
            all_messages.extend(messages)
        return [m for m in all_messages if m['ts'] != oldest]

    def _resolve_channels(self, channels=None):
        """Return a tuple if channel IDs from channels argument

        argument may be a channel ID or name as a string or a list of any
        combination if channel IDs or names

        Returns a tuple of unique (de-duplicated) channel IDs
        """
        if channels is None:
            channels = self.channels.keys()
        elif type(channels) == str:
            channels = [channels]
        result = []
        for channel in channels:
            if channel in self.chan_ids:
                result.append(channel)
            elif channel in self.channels:
                result.append(self.channels[channel])
            else:
                raise AssertionError('"{0}" is an unknown channel'.format(channel))
        return tuple(set(result))

    def speak(self, message=None, paste=False, attachments=None, channel=None):
        """Speak into requested channels

            argument may be a channel ID or name as a string or a list of any
            combination if channel IDs or names. (Default speaks into all
            configured channels.)
        """
        channels = self._resolve_channels(channel)
        for channel in channels:
            if paste:
                message = '```{0}```'.format(message)
            self.slack.chat.post_message(channel, message,
                                         username=self.botname,
                                         icon_emoji=self.botemoji,
                                         icon_url=self.boticon,
                                         attachments=attachments)

    def upload(self, file_, filename=None, title=None, channel=None):
        """Upload a file to channel"""
        channel = self.resolve_channels(channel)
        if title is None:
            title = '(Upload by {})'.format(title, self.botname)
        else:
            title = '{} (Upload by {})'.format(title, self.botname)
        for chan in channel:
            if chan in self.channels:
                chan = self.channels[chan]
            assert chan in self.chan_ids
        self.slack.files.upload(file_, channels=channel,
                            filename=filename,
                            title=title)

    def set_topic(self, topic, channel=None):
        """Set a channel's topic"""
        channel = self._resolve_channels(channel)
        if len(channel) != 1:
            raise SlackardNonFatalError("set_topic only accepts 1 channel")
        self.slack.channels.set_topic(channel=channel[0], topic=topic)

    def channel_info(self, channel=None):
        """Fetch channel's info"""
        channel = self._resolve_channels(channel)
        if len(channel) != 1:
            raise SlackardNonFatalError("channel_info only accepts 1 channel")
        info = self.slack.channels.info(channel=channel[0])
        return info.body['channel']

    def run(self):
        """Message handling loop"""
        self._init_connection()
        self._import_plugins()

        cmd_matcher = re.compile('^{0}:\s*(\S+)\s*(.*)'.format(
                                 self.botnick), re.IGNORECASE)
        h = [self.slack.channels.history(x, count=1) for x in
             self.channels.values()]
        t0 = time.time()
        ts = max([x.body['messages'][0]['ts'] for x in h
                 if x.successful
                 and 'ts' in x.body['messages'][0]])

        if not ts:
            ts = t0

        while True:
            t1 = time.time()
            delta_t = t1 - t0
            if delta_t < 5.0:
                time.sleep(5.0 - delta_t)
            t0 = time.time()

            try:
                messages = self._fetch_messages_since(ts)
            except Exception as e:
                # Possibly an error we can recover from so raise
                # a non-fatal exception and attempt to recover
                raise SlackardNonFatalError(e.message)

            for message in messages:
                ts = message['ts']
                if 'text' in message:
                    # Skip actions on self-produced messages.
                    try:
                        if (message['subtype'] == 'bot_message' and
                                message['username'] == self.botname):
                            continue
                    except KeyError:
                        pass
                    print(message)
                    # Plugins receive full message object in all invocations
                    for f in self.firehoses:
                        f(message)
                    for (f, matcher) in self.subscribers:
                        if matcher.search(message['text']):
                            f(message)
                    m = cmd_matcher.match(message['text'])
                    if m:
                        cmd, args = m.groups()
                        for (f, command) in self.commands:
                            if command == cmd:
                                f(args, message)

    def subscribe(self, pattern):
        """Register subscriptions to messages matching pattern"""
        if hasattr(pattern, '__call__'):
            raise TypeError('Must supply pattern string')

        def real_subscribe(wrapped):
            @functools.wraps(wrapped)
            def _f(*args, **kwargs):
                return wrapped(*args, **kwargs)

            try:
                matcher = re.compile(pattern, re.IGNORECASE)
                self.subscribers.append((_f, matcher))
            except:
                print('Failed to compile matcher for {0}'.format(wrapped))
            return _f

        return real_subscribe

    def command(self, command):
        """register subscriptions to messages starting with 'botnick:'"""
        if hasattr(command, '__call__'):
            raise TypeError('Must supply command string')

        def real_command(wrapped):
            @functools.wraps(wrapped)
            def _f(*args, **kwargs):
                return wrapped(*args, **kwargs)

            self.commands.append((_f, command))
            return _f

        return real_command

    def firehose(self, wrapped):
        """Register subscription to all messages"""
        @functools.wraps(wrapped)
        def _f(*args, **kwargs):
            return wrapped(*args, **kwargs)

        self.firehoses.append(_f)
        return _f


def usage():
    yaml_template = """
    slackard:
        apikey: my_api_key_from-api.slack.com
        channel: random, ...
        botname: Slackard
        botnick: slack  # short form name for commands.
        # Use either boticon or botemoji
        boticon: http://i.imgur.com/IwtcgFm.png
        botemoji: boom
        # plugins directory relative to config file, or absolute
        # create empty __init__.py in that directory
        plugins: ./myplugins
    """
    print('Usage: slackard <config.yaml>')
    print('\nExample YAML\n{}'.format(yaml_template))


def main():
    config_file = None
    try:
        config_file = sys.argv[1]
    except IndexError:
        pass

    if config_file is None:
        usage()
        sys.exit(1)

    if not os.path.isfile(config_file):
        print('Config file "{}" not found.'.format(config_file))
        sys.exit(1)

    try:
        bot = Slackard(config_file)
    except Exception as e:
        print('Encountered error: {}'.format(e.message))
        sys.exit(1)

    while True:
        try:
            bot.run()
        except SlackardFatalError as e:
            print('Fatal error: {}'.format(e.message))
            sys.exit(1)
        except SlackardNonFatalError as e:
            print('Non-fatal error: {}'.format(e.message))
            delay = 5
            print('Delaying for {} seconds...'.format(delay))
            time.sleep(delay)
            bot._init_connection()
        except Exception as e:
            print('Unhandled exception: {}'.format(e.message))
            sys.exit(1)


if __name__ == '__main__':
    main()
