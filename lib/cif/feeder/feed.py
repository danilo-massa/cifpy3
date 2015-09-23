__author__ = 'James DeVincentis <james.d@hexhost.net>'

import os
import datetime
import tempfile
import zipfile

import yaml
import requests

import cif
from .parser import Parser
from ..worker import tasks


class Feed(object):
    def __init__(self, feed_file):
        self.feed_file = feed_file
        self.feed_config = {}
        self.parser = None
        self.logging = cif.logging.getLogger('FEED')

        try:
            self.logging.debug("Opening Feed file for parsing")
            with open(self.feed_file, 'r') as stream:
                self.logging.debug("Parsing feed file")
                feed_config = yaml.load(stream)
        except Exception as e:
            self.logging.error("Could not parse feed file {0}: {1}".format(self.feed_file, e))
            return

        if "feeds" not in feed_config.keys():
            self.logging.info("No feeds configured inside of {0}. Moving on to next file.".format(self.feed_file))
            return

        if "parser" in feed_config.keys():
            for key, value in feed_config["feeds"].items():
                if "parser" not in feed_config["feeds"][key].keys():
                    feed_config["feeds"][key]["parser"] = feed_config["parser"]

        if "defaults" in feed_config.keys():
            for key, value in feed_config["defaults"].items():
                for k, v in feed_config["feeds"].items():
                    if key not in feed_config["feeds"][k].keys():
                        feed_config["feeds"][k][key] = value

        self.feed_config = feed_config

    def process(self):
        """Retrieves the feed, passes it to a parser, and then passes parsed observables to the workers

        :return:
        """
        if "feeds" not in self.feed_config.keys():
            return

        for feed_name, feed in self.feed_config["feeds"].items():
            # These are fields that are used for control when parsing and should not be passed down to the observables
            fields_to_strip = ['node', 'map', 'values', 'pattern', 'remote', 'parser',
                               'username', 'password', 'method', 'start', 'end']

            # Pull out parsing details for feeds from defined meta
            feed_parsing_details = dict((name, feed[name]) for name in fields_to_strip if name in feed.keys())

            # Exclude control fields from defined meta for created observables
            feed_meta = dict((name, feed[name]) for name in feed.keys() if name not in fields_to_strip)

            if "method" not in feed_parsing_details.keys():
                feed_parsing_details["method"] = "GET"

            if "parser" not in feed_parsing_details.keys():
                feed_parsing_details["parser"] = "regex"

            if "values" not in feed_parsing_details.keys():
                feed_parsing_details["values"] = ["observable"]
            elif not isinstance(feed_parsing_details["values"], list):
                feed_parsing_details["values"] = [feed_parsing_details["values"]]

            feed_parsing_details["journal"] = "{0}/{1}-{2}-{3}-journal.pickle".format(
                cif.CACHEDIR, os.path.basename(self.feed_file), feed_name,
                datetime.datetime.utcnow().strftime("%Y-%m-%d")
            ).lower()
            self.logging.debug("Built Journal Path: {0}".format(feed_parsing_details['journal']))

            if feed_parsing_details["remote"].startswith('/'):
                self.logging.debug("Local file detected '{0}'. Opening Locally.".format(feed_parsing_details["remote"]))
                temp = open(feed_parsing_details["remote"], "rb")
            else:
                self.logging.debug("Remote file detected '{0}'. Opening remotely.".format(
                    feed_parsing_details["remote"])
                )
                response = requests.request(feed_parsing_details["method"], feed_parsing_details["remote"],
                                            proxies=cif.proxies, stream=True
                                            )

                if response.status_code > 300:
                    self.logging.error("Failed fetching remote feed '{0}': {1} {2}".format(
                        feed_parsing_details["remote"], response.status_code, response.reason)
                    )
                    return

                temp = tempfile.TemporaryFile(mode="w+b")

                for chunk in response.iter_content(1024 * 1024):
                    temp.write(chunk)

                response.close()

                self.logging.debug("Wrote {0} bytes to binary file.".format(temp.tell()))

                temp.seek(0)

            zip_file = None
            if zipfile.is_zipfile(temp):
                zip_file = zipfile.ZipFile(temp)
                temp_bin = zip_file.open(zip_file.namelist()[0])
            else:
                temp_bin = temp
                temp_bin.seek(0)

            file_to_parse = tempfile.TemporaryFile(mode="w+")
            while True:
                buf = temp_bin.read(1024 * 1024)
                if len(buf) == 0:
                    break
                file_to_parse.write(buf.decode("UTF-8"))
            self.logging.debug("Wrote {0} bytes to text file.".format(file_to_parse.tell()))
            file_to_parse.seek(0)

            if zip_file is not None:
                zip_file.close()
            temp_bin.close()

            self.logging.debug("Creating Parser for feed {0}".format(feed_parsing_details['remote']))
            self.parser = Parser(parsing_details=feed_parsing_details, basemeta=feed_meta, file=file_to_parse)

            while self.parser.parsing:
                observables = self.parser.parsefile(2000)
                if len(observables) > 0:
                    self.logging.debug("Feed '{0}' is sending {1} new objects to be processed".format(
                        feed_parsing_details['remote'], len(observables))
                    )
                    for observable in observables:
                        tasks.put(observable)

            file_to_parse.close()
            self.logging.debug("Finished Parsing feed {0}".format(feed_parsing_details['remote']))