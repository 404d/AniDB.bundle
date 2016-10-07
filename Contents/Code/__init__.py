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
import sys
from datetime import datetime, timedelta

ANIDB_PIC_URL_BASE = "http://img7.anidb.net/pics/anime/"

IDLE_TIMEOUT = timedelta(seconds=60 * 5)

LOCK = threading.RLock()

CONNECTION = None
LAST_ACCESS = None

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


def checkConnection():
    """Plugin agent for managing the API session."""
    global LAST_ACCESS
    global CONNECTION

    Log("Checking for idle connection timeout...")

    LOCK.acquire()
    try:
        if CONNECTION is not None and LAST_ACCESS is not None \
                and (datetime.now() - IDLE_TIMEOUT) > LAST_ACCESS:
            CONNECTION.stop()
            CONNECTION = None
            Log("Connection timeout reached. Closing connection!")
    except:
        pass
    finally:
        LOCK.release()

    if CONNECTION is not None:
        Thread.CreateTimer(300, checkConnection)


class MotherAgent:
    "Base metadata agent with utility functions for loading data from AniDB."

    def connect(self):
        "Create an API session and authenticate with the stored credentials."

        global CONNECTION
        global LAST_ACCESS

        try:
            username = Prefs["username"]
            password = Prefs["password"]

            if CONNECTION is not None:
                if not CONNECTION.authed():
                    CONNECTION.auth(username, password)

                Log("Reusing authenticated connection")
                LAST_ACCESS = datetime.now()
                return CONNECTION

            CONNECTION = adba.Connection(log=True)

            Thread.CreateTimer(300, checkConnection)

            if not username or not password:
                    Log("Set username and password!")
                    return None

            CONNECTION.auth(username, password)
            Log("Auth ok!")

        except Exception:
            Log("Connection exception, traceback:")
            Log("".join(traceback.format_exception(sys.exc_type,
                                                   sys.exc_value,
                                                   sys.exc_traceback)))
            raise Exception("See INFO-level message above for traceback")

        LAST_ACCESS = datetime.now()
        return CONNECTION

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

    def getDescription(self, connection, aid, part):
        "Return one 1400-byte `part` of the description for AniDB anime `aid`."

        animeDesc = adba.AnimeDesc(connection, aid=aid, part=part)
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
            desc = desc + self.getDescription(connection, aid, part + 1)

        # We only want to clean up once we've finished fetching the show desc.
        if part != 0:
            return desc

        desc = desc.replace("<br />", "\n").replace("`", "'")
        desc = self.decodeString(desc)

        # Clean description
        lines = []
        for line in desc.split("\n"):
            # Remove info-lines
            patterns = [
                "*",
                "Note: ",
                "Source: ",
            ]

            if [True for pattern in patterns if line.startswith(pattern)]:
                continue

            lines.append(line)

        desc = "\n".join(lines).strip("\n")

        return desc

    def getValueWithFallbacks(self, dictionary, *names):
        """Return the value of the first non-empty `names`-element found in
        `dictionary`.
        """

        for name in names:
            if name in dictionary and len(dictionary[name]) > 0:
                return dictionary[name]

        return None

    def getDate(self, timestampString):
        """Return a datetime-object initialized with the given Unix timestamp.
        """
        return datetime.fromtimestamp(int(timestampString))

    def getAnimeInfo(self, connection, aid, metadata, movie=False,
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

        anime = adba.Anime(connection, aid=metadata.id,
                           paramsA=["epno", "english_name", "kanji_name",
                                    "romaji_name", "year", "picname", "url",
                                    "rating", "episodes",
                                    "tag_weight_list", "tag_name_list",
                                    "highest_episode_number", "air_date"])
        try:
            anime.load_data()
        except Exception, e:
            Log("Could not load anime info, msg: " + str(e))
            raise e

        try:
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

            metadata.title = self.getValueWithFallbacks(anime.dataDict,
                                                        titleKey(),
                                                        'english_name',
                                                        'romaji_name',
                                                        'kanji_name')

            metadata.originally_available_at = self.getDate(
                anime.dataDict['air_date'])

            if "tag_name_list" in anime.dataDict:
                min_weight = int(float(Prefs["tag_min_weight"]) * 200)
                weights = anime.dataDict["tag_weight_list"].split(",")
                genres = anime.dataDict["tag_name_list"].split(",")
                # Can't assign containers in Plex API
                for (genre, weight) in zip(genres, weights):
                    if int(weight) >= min_weight:
                        metadata.genres.add(genre)
                    else:
                        Log("Skipping tag '%s': Weight is %i, minimum "
                            "weight is %i."
                            % (genre, int(weight), min_weight))

            if "picname" in anime.dataDict:
                picUrl = ANIDB_PIC_URL_BASE + anime.dataDict['picname']
                poster = Proxy.Media(HTTP.Request(picUrl).content)
                metadata.posters[picUrl] = poster

        except Exception, e:
            Log("Could not set anime metadata, msg: " + str(e))
            raise e

        try:
            metadata.summary = self.getDescription(connection, metadata.id, 0)
        except Exception, e:
            sys.excepthook(*sys.exc_info())
            Log("Could not load description, msg: " + str(e))
            raise e

    def doHashSearch(self, results, filename, connection):
        """Return an AniDB file entry, given the path to a local file."""

        filePath = urllib.unquote(filename)

        fileInfo = adba.File(connection, filePath=filePath, paramsF=["aid"],
                             paramsA=["english_name", "romaji_name",
                                      "kanji_name", "year"])

        try:
            Log("Trying to lookup %s by file on anidb" % filePath)
            fileInfo.load_data()
        except Exception, e:
            Log("Could not load file data, msg: " + str(e))

        return fileInfo

    def doNameSearch(self, results, name, connection):
        """Return an AniDB anime entry, given a search string for the name
        field.
        """

        fileInfo = adba.Anime(connection, name=name,
                              paramsA=["english_name", "kanji_name",
                                       "romaji_name", "year", "aid"])
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

        connection = self.connect()

        if connection is None:
            return

        fileInfo = None

        if media.filename is not None:
            fileInfo = self.doHashSearch(results, media.filename, connection)

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

            fileInfo = self.doNameSearch(results, metaName, connection)

        if "aid" not in fileInfo.dataDict:
            Log("No match found or error occurred!")
            return

        aid = fileInfo.dataDict['aid']

        name = self.getValueWithFallbacks(fileInfo.dataDict, titleKey(),
                                          'english_name', 'romaji_name',
                                          'kanji_name')

        year = str(fileInfo.dataDict['year'])
        if year.find('-') > -1:
            year = year[:year.find('-')]

        Log("Appending metadata search result for anime " + name)

        results.Append(MetadataSearchResult(id=str(aid), name=name,
                                            year=int(year), score=100,
                                            lang=Locale.Language.English))


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
        connection = self.connect()
        if not connection:
            return

        self.getAnimeInfo(connection, metadata.id, metadata, True, force)


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

        connection = self.connect()
        if not connection:
            return

        self.getAnimeInfo(connection, metadata.id, metadata, False, force)

        for s in media.seasons:

            for picUrl in metadata.posters.keys():
                metadata.seasons[s].posters[picUrl] = Proxy.Media(
                    HTTP.Request(picUrl).content)

            for ep in media.seasons[s].episodes:

                episodeKey = self.loadEpisode(connection, metadata, s, ep,
                                              force)

                episode = metadata.seasons[s].episodes[ep]
                episode.title = Dict[episodeKey + "title"]

                if str(episodeKey + "rating") in Dict:
                    episode.rating = Dict[episodeKey + "rating"]

                if str(episodeKey + "length") in Dict:
                    episode.duration = Dict[episodeKey + "length"]

                if str(episodeKey + "aired") in Dict:
                    aired = Dict[episodeKey + "aired"]
                    episode.originally_available_at = aired

    def loadEpisode(self, connection, metadata, season, episode, force):

        epno = episode
        if str(season) == "0":
            epno = "S" + str(epno)

        episodeKey = ("aid:" + str(metadata.id) + "-" + str(season) + "-"
                      + str(episode) + "-")

        Log("Force: " + str(force))
        Log("Has key: " + str(str(episodeKey + "title") in Dict))

        if str(episodeKey + "title") in Dict and not force:
            Log("Metadata for '" + metadata.title + "', season " + season +
                " episode " + epno + " found in cache")
            return episodeKey

        Log("Loading metadata for '" + metadata.title + "', season " +
            season + " episode " + epno + " from AniDB")

        episode = adba.Episode(connection, aid=metadata.id, epno=epno)

        try:
            episode.load_data()
        except IndexError, e:
            Log("Episode number is incorrect, msg: " + str(e) +
                " for episode " + epno)
        except Exception, e:
            Log("Could not load episode info, msg: " + str(e))
            raise e

        Dict[episodeKey + "title"] = self.getValueWithFallbacks(
            episode.dataDict, titleKey(), 'english_name', 'romaji_name',
            'kanji_name')

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
