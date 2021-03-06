# -*- encoding: utf-8 -*-
if None:
    from nonexistent import HTTP, Prefs, Thread, Proxy, MetadataSearchResult  # NOQA
    from nonexistent import Locale, Agent, Dict

    def Log(msg):
        print msg
else:
    Log("Running under Plex")

import adba
import urllib
import threading
import traceback
import re
import sys
from datetime import datetime, timedelta

ANIDB_PIC_URL_BASE = "https://cdn-eu.anidb.net/images/main/"
OLD_ANIDB_PIC_URL_BASE = "http://img7.anidb.net/pics/anime/"

IDLE_TIMEOUT = timedelta(seconds=60 * 30)

LOCK = threading.RLock()

CONNECTION = None
LAST_ACCESS = None
LAST_COOLDOWN = None
INITIAL_COOLDOWN = timedelta(hours=1)
COOLDOWN_CAP = timedelta(hours=48)

LANGUAGE_MAP = dict()


def exception_hook(*args):
    Log("".join(traceback.format_exception(*args)))

sys.excepthook = exception_hook


def thread_lock(func):
    "Automatically handle thread locking when calling the decorated function."

    def do_call(*args, **kwargs):
        try:
            LOCK.acquire()
            func(*args, **kwargs)

        finally:
            LOCK.release()

    return do_call


def Start():
    """Plex Plugin entrypoint."""
    HTTP.CacheTime = 3600
    LANGUAGE_MAP["English"] = "english_name"
    LANGUAGE_MAP["Romaji"] = "romaji_name"
    LANGUAGE_MAP["Kanji"] = "kanji_name"


def titleKey():
    """Utility method for finding the key name of the currently user-selected
    language that should be used for loading show and episode names."""
    titlePref = Prefs["title_lang"]
    return LANGUAGE_MAP[titlePref]


def sortKey():
    """Utility method for finding the key name of the currently user-selected
    language that should be used for loading sort title for show and episode
    names."""
    sortPref = Prefs["title_sort_lang"]

    if sortPref == "---":
        # The title key has already been looked up in the language map, so
        # just gonna return that.
        return titleKey()

    return LANGUAGE_MAP[sortPref]


def checkConnection():
    """Plugin agent for managing the API session."""
    global LAST_ACCESS
    global CONNECTION

    Log("Checking for idle connection timeout...")

    LOCK.acquire()
    try:
        if CONNECTION is not None and LAST_ACCESS is not None \
                and (datetime.now() - IDLE_TIMEOUT) > LAST_ACCESS \
                and not CONNECTION.banned:
            CONNECTION.stop()
            CONNECTION = None
            Log("Connection timeout reached. Closing connection!")
    except:
        pass
    finally:
        LOCK.release()

    if CONNECTION is not None:
        Thread.CreateTimer(300, checkConnection)


def callStack():
    for line in traceback.format_stack():
        Log(line.strip())


class MotherAgent:
    "Base metadata agent with utility functions for loading data from AniDB."

    @property
    def connection(self):
        "Create an API session and authenticate with the stored credentials."

        global CONNECTION
        global LAST_ACCESS
        global LAST_COOLDOWN

        try:
            username = Prefs["username"]
            password = Prefs["password"]

            if CONNECTION is not None:
                # Take care of ban handling
                if self.is_banned:
                    # Initialize the cooldown timer if neccessary
                    if not CONNECTION.ban_cooldown:
                        # Set cooldown
                        if LAST_COOLDOWN:
                            LAST_COOLDOWN = LAST_COOLDOWN * 2
                        else:
                            LAST_COOLDOWN = INITIAL_COOLDOWN

                        # Constrain the max cooldown time
                        if LAST_COOLDOWN > COOLDOWN_CAP:
                            LAST_COOLDOWN = COOLDOWN_CAP

                        Log("Ban cooldown: %r" % LAST_COOLDOWN)
                        CONNECTION.ban_cooldown = datetime.utcnow() + LAST_COOLDOWN
                        Log("Ban cooldown expires on %r" % CONNECTION.ban_cooldown)

                    # Clear ban timer if cooldown is over
                    if not CONNECTION.ban_cooldown_active:
                        Log("Banned but cooldown over, clearing ban state")
                        CONNECTION.link.banned = False

                # Perform connection
                if not self.is_banned and not CONNECTION.authed():
                    Log("Authenticating")
                    CONNECTION.auth(username, password)

                elif self.is_banned:
                    Log("Banned, will return current connection")

                else:
                    Log("Reusing authenticated connection")
                    LAST_ACCESS = datetime.now()

                return CONNECTION

            CONNECTION = adba.Connection(log=True, keepAlive=True)

            Thread.CreateTimer(60, checkConnection)

            if not username or not password:
                    raise Exception("Set username and password!")

            CONNECTION.auth(username, password)
            Log("Auth ok!")

        except Exception:
            Log("Connection exception, traceback:")
            Log("".join(traceback.format_exception(*sys.exc_info())))

        LAST_ACCESS = datetime.now()
        return CONNECTION

    @property
    def is_banned(self):
        if CONNECTION and CONNECTION.link:
            return CONNECTION.link.banned

        return False

    def decodeString(self, string=None):
        """"Decode" and return the given string.

        From what I can see this function is used for stripping HTML and
        BBcode-tags from e.g. descriptions and the like.
        It recursively calls itself, gradually removing a matching set of <>
        and [].
        """

        if string is None:
            return string

        # Look for a BBcode tag, remove it and recurse.
        bracketStart = string.find('[')
        bracketEnd = string.find(']')
        if bracketStart > -1 and bracketEnd > bracketStart:
            string = string[:bracketStart] + string[bracketEnd + 1:]
            string = self.decodeString(string)

        # Look for an HTML tag, remove it and recurse.
        lt = string.find('<')
        gt = string.find('>')
        if lt > -1 and gt > lt:
            string = string[:lt] + string[gt + 1:]
            string = self.decodeString(string)

        return string

    def getDescription(self, aid, part):
        "Return one 1400-byte `part` of the description for AniDB anime `aid`."

        Log("Description stuff")
        animeDesc = adba.AnimeDesc(self.connection, aid=aid, part=part)

        cacheKey = "aid:%s:desc" % aid
        if part == 0 and cacheKey in Dict \
                and not isinstance(Dict[cacheKey], dict):
            Log("Loading desc from cache key %s" % cacheKey)
            return Dict[cacheKey]

        Log("Cache miss for key %s" % cacheKey)

        if self.is_banned:
            Log("Banned from the API and with no cache, returning no desc data")
            return None

        Log("Loading desc from API for key %s" % cacheKey)

        try:
            animeDesc.load_data()
        except IndexError:
            # This should occurr when we get status code 333
            Log("No description found for anime aid " + aid)
            return None

        if "description" not in animeDesc.dataDict:
            Log("No description found for anime aid " + aid)
            return None

        desc = animeDesc.dataDict["description"]
        currentPart = int(animeDesc.dataDict['current_part'])
        maxParts = int(animeDesc.dataDict['max_parts'])

        if (maxParts - currentPart) > 1:
            desc = desc + self.getDescription(aid, part + 1)

        # We only want to clean up once we've finished fetching the show desc.
        if part != 0:
            return desc

        # FIXME: I fucked up cache, should be unfucked then remove this
        if isinstance(desc, list):
            desc = "'".join(desc)

        desc = desc.replace("<br />", "\n").replace("`", "'")
        desc = self.decodeString(desc)

        # Clean description
        lines = []
        for line in desc.split("\n"):
            # Remove info-lines
            patterns = [
                lambda x: x.startswith("*"),
                lambda x: re.match("^Note( \d+)?:", x),
                lambda x: x.startswith("Source: "),
                lambda x: x.startswith(u"— written by"),
                lambda x: x.startswith("~ translated"),
            ]

            if [True for pattern in patterns if pattern(line)]:
                continue

            lines.append(line)

        desc = "\n".join(lines).strip("\n")

        Dict[cacheKey] = desc

        return desc

    def getValueWithFallbacks(self, dictionary, *names):
        """Return the value of the first non-empty `names`-element found in
        `dictionary`.
        """

        for name in names:
            if name in dictionary and dictionary[name]:
                return dictionary[name]

        return None

    def getDate(self, timestampString):
        """Return a datetime-object initialized with the given Unix timestamp.
        """
        return datetime.fromtimestamp(int(timestampString))

    def getTitles(self, metadata):
        """Return the title and sort title for a given object based on user
        settings and fallback rules."""
        fallback = ["english_name", "romaji_name", "kanji_name"]
        title = self.getValueWithFallbacks(metadata, titleKey(), *fallback)
        sort = self.getValueWithFallbacks(metadata, sortKey(), *fallback)

        # FIXME: Handle fuck-ups in which apostrophes were cached and will fuck up cache lookups due to auto splitting lists
        if isinstance(title, list):
            title = "'".join(title)

        if isinstance(sort, list):
            sort = "'".join(sort)

        # "Unescape"
        # There's few enough cases of legitimate use of ` in titles that I
        # don't care this ain't gonna be accurate.
        if title:
            title = title.replace("`", "'")

        if sort:
            sort = sort.replace("`", "'")
        else:
            sort = title

        return (title, sort)


    def getAnimeInfo(self, aid, metadata, movie=False,
                     force=False):
        """Return a Plex metadata instance for the AniDB anime `aid`.

        Returns a Plex metadata object with the following fields filled out:
            - Air date (/ Year - movies)
            - Show name, in the language indicated by the `titleKey` method
            - Rating
            - Poster
            - Show Description (via the `getDescription` method)
        """

        Log("Loading metadata for anime aid " + aid)

        anime = adba.Anime(self.connection, aid=metadata.id,
                           paramsA=["epno", "english_name", "kanji_name",
                                    "romaji_name", "other_name", "year",
                                    "picname", "url", "rating", "episodes",
                                    "tag_weight_list", "tag_name_list",
                                    "highest_episode_number", "air_date"])

        cacheKey = "aid:%s" % metadata.id
        if cacheKey not in Dict or force:
            Log("Cache miss for key %s" % cacheKey)

            if self.is_banned:
                Log("Banned from the API and with no cache, returning no anime data")
                return None

            anime.load_data()
            Dict[cacheKey] = anime.dataDict
            Log("Anime dump: %r" % (anime.dataDict, ))

            if cacheKey + ":desc" in Dict:
                Log("Cleaning up key %s:desc" % cacheKey)
                del Dict[cacheKey + ":desc"]
        else:
            Log("Loading anime info from cache key %s" % cacheKey)
            anime.dataDict = Dict[cacheKey]

        if movie and "year" in anime.dataDict:
            year = str(anime.dataDict['year'])
            if year.find('-') > -1:
                year = year[:year.find('-')]
            try:
                metadata.year = int(year)
            except:
                pass

        if "rating" in anime.dataDict:
            metadata.rating = float(anime.dataDict['rating']) / 100

        (metadata.title, metadata.title_sort) = self.getTitles(anime.dataDict)

        metadata.originally_available_at = self.getDate(
            anime.dataDict['air_date'])

        if "tag_name_list" in anime.dataDict and \
                anime.dataDict["tag_name_list"]:
            min_weight = int(float(Prefs["tag_min_weight"]) * 200)
            # Cast to str since adba type handling is shit
            # If the response field can be parsed as an int, it'll be parsed
            # as an int
            # This breaks one element tag lists
            weights = str(anime.dataDict["tag_weight_list"]).split(",")
            genres = anime.dataDict["tag_name_list"]

            if not isinstance(anime.dataDict["tag_name_list"], list):
                genres = genres.split(",")

            # Bug fix for bad '-escaping
            elif [True for item in anime.dataDict["tag_name_list"] if u"," in item]:
                Log("Attempting to fix bug with cached tag data being incorrectly split")
                genres = anime.dataDict["tag_name_list"]
                Log("Before: %r" % (genres, ))
                genres = u"'".join(genres).split(u",")
                Log("After: %r" % (genres, ))

                Log("Looks like that worked. Saving fixed cache data")
                anime.dataDict["tag_name_list"] = genres
                Dict[cacheKey] = anime.dataDict


            Log(repr(genres))

            # Can't assign containers in Plex API
            metadata.genres.clear()
            for (weight, genre) in sorted(zip(weights, genres)):
                if int(weight) >= min_weight:
                    metadata.genres.add(genre)
                else:
                    Log("Skipping tag '%s': Weight is %i, minimum "
                        "weight is %i."
                        % (genre, int(weight), min_weight))

        # Replace old CDN photos
        new_posters = {}
        delete_posters = set()
        for url in metadata.posters:
            if url.startswith(OLD_ANIDB_PIC_URL_BASE):
                Log("Updating CDN URL for %r" % url)
                # Fix URL
                new_url = url.replace(OLD_ANIDB_PIC_URL_BASE, ANIDB_PIC_URL_BASE)
                # Prep data
                delete_posters.add(url)
                new_posters[new_url] = Proxy.Media(metadata.posters[url])

        # Add new
        for k, v in new_posters.items():
            metadata.posters[k] = v

        # Delete old
        for url in delete_posters:
            del metadata.posters[url]

        if "picname" in anime.dataDict:
            picUrl = ANIDB_PIC_URL_BASE + anime.dataDict['picname']

            if picUrl not in metadata.posters:
                poster = Proxy.Media(HTTP.Request(picUrl).content)
                metadata.posters[picUrl] = poster

        metadata.summary = self.getDescription(metadata.id, 0)

    def doHashSearch(self, results, filename):
        """Return an AniDB file entry, given the path to a local file."""

        filePath = urllib.unquote(filename)

        fileInfo = adba.File(self.connection, filePath=filePath, paramsF=["aid"],
                             paramsA=["english_name", "romaji_name",
                                      "kanji_name", "year"])

        if self.is_banned:
            Log("Banned from the API and with no cache, returning no hash search data")
            return None

        try:
            Log("Trying to lookup %s by file on anidb" % filePath)
            fileInfo.load_data()
        except Exception, e:
            Log("Could not load file data, msg: " + str(e))

        return fileInfo

    def doNameSearch(self, results, name):
        """Return an AniDB anime entry, given a search string for the name
        field.
        """

        fileInfo = adba.Anime(self.connection, name=name,
                              paramsA=["english_name", "kanji_name",
                                       "romaji_name", "other_name", "year",
                                       "aid"])

        if self.is_banned:
            Log("Banned from the API and with no cache, returning no name search data")
            return None

        try:
            Log("Trying to lookup %s by name on anidb" % name)
            fileInfo.load_data()
        except Exception, e:
            Log("Could not load anime data, msg: " + str(e))
            raise e

        return fileInfo

    def doSearch(self, results, media, lang):
        """Look up metadata for a Plex media object.

        NOTE:
        The results field is an array made by the caller, in which we're
        supposed to push our result objects. Not very pythonic, or amirite
        """

        fileInfo = None

        if media.filename is not None:
            fileInfo = self.doHashSearch(results, media.filename)

        if not fileInfo or (
                "aid" not in fileInfo.dataDict and (media.name or media.show)):
            metaName = media.name
            if metaName is None:
                metaName = media.show

            if metaName is not None and metaName.startswith('aid:'):
                aid = metaName[4:].strip()
                Log("Will search for metadata for anime id " + aid)
                result = MetadataSearchResult(id=str(aid), name=metaName,
                                              year=None, score=100,
                                              lang=Locale.Language.English)
                results.Append(result)
                return

            fileInfo = self.doNameSearch(results, metaName)

        if not fileInfo or "aid" not in fileInfo.dataDict:
            Log("No match found or error occurred!")
            return

        aid = fileInfo.dataDict['aid']

        name = self.getTitles(fileInfo.dataDict)[0]

        year = str(fileInfo.dataDict['year'])
        if year.find('-') > -1:
            year = year[:year.find('-')]

        Log("Appending metadata search result for anime " + name)

        results.Append(MetadataSearchResult(id=str(aid), name=name,
                                            year=int(year), score=100,
                                            lang=Locale.Language.English))

    def getEpisodeFromCache(self, episodeKey):
        keys = ["english_name", "romaji_name", "kanji_name", "length",
                "rating", "aired"]

        episode = {}
        for key in keys:
            if (episodeKey + key) in Dict:
                episode[key] = Dict[episodeKey + key]
            else:
                episode[key] = None

        return episode


class AniDBAgentMovies(Agent.Movies, MotherAgent):
    """Specialized AniDB metadata agent for Plex Movie libraries."""

    name = 'AniDB'
    primary_provider = True
    languages = [Locale.Language.English]
    accepts_from = ['com.plexapp.agents.localmedia',
                    'com.plexapp.agents.opensubtitles']

    @thread_lock
    def search(self, results, media, lang):
        # TODO: Move me into the MotherAgent class
        self.doSearch(results, media, lang)

    @thread_lock
    def update(self, metadata, media, lang, force=None):
        # TODO: Move me into the MotherAgent class

        if force is None:
            force = Prefs["skip_cache"]
            Log("Caller didn't specify whether to skip cache. User pref: %s" %
                force)

        self.doUpdate(metadata, media, lang, force)

    def doUpdate(self, metadata, media, lang, force):
        self.getAnimeInfo(metadata.id, metadata, True, force)


class AniDBAgentTV(Agent.TV_Shows, MotherAgent):
    """Specialized AniDB metadata agent for Plex TV Show libraries."""

    name = 'AniDB'
    primary_provider = True
    languages = [Locale.Language.English]
    accepts_from = ['com.plexapp.agents.localmedia',
                    'com.plexapp.agents.opensubtitles']

    @thread_lock
    def search(self, results, media, lang):
        # TODO: Move me into the MotherAgent class
        self.doSearch(results, media, lang)

    @thread_lock
    def update(self, metadata, media, lang, force=None):
        # TODO: Move me into the MotherAgent class

        if force is None:
            force = Prefs["skip_cache"]
            Log("Caller didn't specify whether to skip cache. User pref: %s" %
                force)

        self.doUpdate(metadata, media, lang, force)

    def doUpdate(self, metadata, media, lang, force):
        self.getAnimeInfo(metadata.id, metadata, False, force)

        for s in media.seasons:

            for picUrl in metadata.posters.keys():
                metadata.seasons[s].posters[picUrl] = Proxy.Media(
                    HTTP.Request(picUrl).content)

            for ep in media.seasons[s].episodes:

                episodeKey = self.loadEpisode(metadata, s, ep, force)
                if not episodeKey:
                    Log("Unable to find data for season %r episode %r" %(s, ep))
                    continue

                episode = metadata.seasons[s].episodes[ep]
                episodeData = self.getEpisodeFromCache(episodeKey)

                episode.title = self.getTitles(episodeData)[0]
                episode.rating = episodeData["rating"]
                episode.duration = episodeData["length"]
                episode.originally_available_at = episodeData["aired"]

    def loadEpisode(self, metadata, season, episode, force):

        epno = episode
        if str(season) == "0":
            epno = "S" + str(epno)

        episodeKey = ("aid:" + str(metadata.id) + "-" + str(season) + "-"
                      + str(episode) + "-")

        Log("Force: " + str(force))
        Log("Has key: " + str(str(episodeKey + "kanji_name") in Dict))

        if str(episodeKey + "kanji_name") in Dict and not force:
            Log("Metadata for '" + metadata.title + "', season " + season +
                " episode " + epno + " found in cache")
            return episodeKey

        Log("Loading metadata for '" + metadata.title + "', season " +
            season + " episode " + epno + " from AniDB")

        if episodeKey in Dict:
            Log("Found in cache, skipping")
            return episodeKey

        if self.is_banned:
            Log("Banned from the API and with no cache, returning no episode data for %r" % episodeKey)
            return None

        episode = adba.Episode(self.connection, aid=metadata.id, epno=epno)

        try:
            episode.load_data()
        except IndexError, e:
            Log("Episode number is incorrect, msg: " + str(e) +
                " for episode " + epno)
        except Exception, e:
            Log("Could not load episode info, msg: " + str(e))
            raise e

        # FIXME: Cache shit lists
        if "english_name" in episode.dataDict:
            if isinstance(episode.dataDict["english_name"], list):
                episode.dataDict["english_name"] = "'".join(episode.dataDict["english_name"])
            Dict[episodeKey + "english_name"] = episode.dataDict["english_name"].replace("`", "'")
        if "romaji_name" in episode.dataDict:
            if isinstance(episode.dataDict["romaji_name"], list):
                episode.dataDict["romaji_name"] = "'".join(episode.dataDict["romaji_name"])
            Dict[episodeKey + "romaji_name"] = episode.dataDict["romaji_name"].replace("`", "'")
        if "kanji_name" in episode.dataDict:
            if isinstance(episode.dataDict["kanji_name"], list):
                episode.dataDict["kanji_name"] = "'".join(episode.dataDict["kanji_name"])
            Dict[episodeKey + "kanji_name"] = episode.dataDict["kanji_name"].replace("`", "'")

        if "rating" in episode.dataDict:
            rating = float(episode.dataDict['rating']) / 100
            Dict[episodeKey + "rating"] = rating

        if "length" in episode.dataDict:
            length = int(episode.dataDict['length']) * 60 * 1000
            Dict[episodeKey + "length"] = length

        if "aired" in episode.dataDict:
            try:
                aired = self.getDate(episode.dataDict['aired'])
                Dict[episodeKey + "aired"] = aired
            except:
                pass

        return episodeKey
