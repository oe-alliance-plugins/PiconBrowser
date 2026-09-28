from hashlib import md5
from os.path import exists, islink, join
from os import remove, symlink


from twisted.internet import reactor
from twisted.internet.defer import Deferred, inlineCallbacks
from twisted.internet.protocol import Protocol
from twisted.internet.threads import deferToThread
from twisted.web.client import Agent, BrowserLikeRedirectAgent, ResponseDone, readBody
from twisted.web.http_headers import Headers

from Components.ActionMap import HelpableActionMap
from Components.config import config, ConfigSelection, ConfigSubsection, ConfigYesNo
from Components.Label import Label
from Components.Pixmap import Pixmap
from Components.Sources.StaticText import StaticText
from Plugins.Plugin import PluginDescriptor
from Screens.LocationBox import LocationBox
from Screens.Screen import Screen
from Screens.Setup import Setup
from Tools.Directories import resolveFilename, SCOPE_CURRENT_PLUGIN

from . import _, PluginLanguageDomain
from .BouquetParser import BouquetParser, getChannelKey, getCleanFileName

PLUGIN_NAME = "PiconBrowser"
BOUQUET_PATH = "/etc/enigma2"
MAX_SETS = 4
SCHEDULER_TIMER_KEY = "PiconBrowserSync"

# GitHub Pages base URL of every available picon repo. More may be added later.
REPOS = {
	"100-light-transparent": "https://piconbrowser.github.io/100-light-transparent",
	"100-dark-transparent": "https://piconbrowser.github.io/100-dark-transparent",
	"220-light-transparent": "https://piconbrowser.github.io/220-light-transparent",
	"220-dark-transparent": "https://piconbrowser.github.io/220-dark-transparent",
	"400-light-transparent": "https://piconbrowser.github.io/400-light-transparent",
	"400-dark-transparent": "https://piconbrowser.github.io/400-dark-transparent",
}

# The repos don't publish a dedicated preview image, so a channel picon that's present in
# every repo is used as a style sample instead.
PREVIEW_IMAGE_NAME = "prosieben.png"
PREVIEW_TMP_PATH = "/tmp/piconbrowser_preview.png"

config.plugins.PiconBrowser = ConfigSubsection()
# Read-only placeholder (single blank choice) for the setup entry that explains the
# Scheduler needs a task to run the sync - Setup's addItem() drops ConfigNothing items.
config.plugins.PiconBrowser.schedulerInfo = ConfigSelection(default="", choices=[("", "")])
config.plugins.PiconBrowser.excludeIptv = ConfigYesNo(default=True)
config.plugins.PiconBrowser.excludeRadio = ConfigYesNo(default=False)
for index in range(MAX_SETS):
	section = ConfigSubsection()
	section.repo = ConfigSelection(default="220-light-transparent", choices=[(name, name) for name in REPOS])
	setattr(config.plugins.PiconBrowser, f"sets{index}", section)


def getActivePiconPaths():
	# Picon target directories are owned by the core Picon settings (config.picon), not by
	# this plugin - it only picks a repo per path that's actually in use for rendering.
	paths = [config.picon.set0.path.value]
	if config.picon.mode.value == 1:
		for index in range(1, MAX_SETS):
			path = getattr(config.picon, f"set{index}").path.value
			if path:
				paths.append(path)
	return paths


class PiconBrowserSetup(Setup):
	def __init__(self, session):
		Setup.__init__(self, session, "PiconBrowser", plugin="Extensions/PiconBrowser", PluginLanguageDomain=PluginLanguageDomain)
		self["key_blue"] = StaticText(_("Sync now"))
		self["infoActions"] = HelpableActionMap(self, ["InfoActions"], {
			"info": (self.showPreview, _("Show a preview image for each picon repo")),
		}, prio=0)
		self["colorActions"] = HelpableActionMap(self, ["ColorActions"], {
			"blue": (self.startManualSync, _("Start a manual picon sync now")),
		}, prio=0)

	def keySave(self):
		config.plugins.PiconBrowser.save()
		Setup.keySave(self)

	def showPreview(self):
		self.session.open(PiconBrowserPreview)

	def startManualSync(self):
		if not PiconBrowser.getInstance(self.session).download():
			self.session.showError(_("A picon sync is already running."))
		else:
			self.session.showInfo(_("Picon sync started in the background."))

	def keySelect(self):
		if self.getCurrentItem() is config.picon.set0.path:
			self.openLocationBox()
		else:
			Setup.keySelect(self)

	def openLocationBox(self):
		def callback(path):
			if path is not None:
				config.picon.set0.path.value = path
			self["config"].invalidateCurrent()

		self.session.openWithCallback(
			callback,
			LocationBox,
			windowTitle=_("Select Picon Directory"),
			text=_("What do you want to set as the picon location?"),
			currDir=config.picon.set0.path.value or "/usr/share/enigma2/picon/",
			bookmarks=config.picon.allowedPaths,
			editDir=True,
		)


class _PiconFileSaver(Protocol):
	"""Writes a response body straight to a local file as it arrives."""

	def __init__(self, finished, path):
		self.finished = finished
		self.file = open(path, "wb")

	def dataReceived(self, data):
		self.file.write(data)

	def connectionLost(self, reason):
		self.file.close()
		if reason.check(ResponseDone):
			self.finished.callback(None)
		else:
			self.finished.errback(reason)


class PiconBrowserPreview(Screen):
	skin = """
	<screen name="PiconBrowserPreview" position="center,center" size="720,540" title="Picon Preview">
		<widget name="preview" position="10,10" size="700,470" alphatest="blend" />
		<widget name="label" position="10,490" size="700,40" font="Regular;22" halign="center" valign="center" />
	</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self.repoNames = list(REPOS)
		self.index = 0
		self["preview"] = Pixmap()
		self["label"] = Label()
		self["actions"] = HelpableActionMap(self, ["DirectionActions", "OkCancelActions"], {
			"left": (self.showPrevious, _("Show the previous repo's preview")),
			"right": (self.showNext, _("Show the next repo's preview")),
			"cancel": (self.close, _("Close the preview")),
			"ok": (self.close, _("Close the preview")),
		}, prio=0)
		self.onLayoutFinish.append(self.showCurrent)

	def showPrevious(self):
		self.index = (self.index - 1) % len(self.repoNames)
		self.showCurrent()

	def showNext(self):
		self.index = (self.index + 1) % len(self.repoNames)
		self.showCurrent()

	def showCurrent(self):
		name = self.repoNames[self.index]
		self["label"].setText(name)
		if not PREVIEW_IMAGE_NAME:
			return
		url = f"{REPOS[name]}/{PREVIEW_IMAGE_NAME}"
		agent = BrowserLikeRedirectAgent(Agent(reactor))
		deferred = agent.request(b"GET", url.encode("utf-8"), Headers({"user-agent": ["PiconBrowser"]}))
		deferred.addCallback(self._gotResponse).addErrback(self._downloadFailed)

	def _gotResponse(self, response):
		if response.code != 200:
			print(f"[{PLUGIN_NAME}] preview not available: HTTP {response.code}")
			return
		finished = Deferred()
		response.deliverBody(_PiconFileSaver(finished, PREVIEW_TMP_PATH))
		finished.addCallback(self._showImage)

	def _showImage(self, _result):
		self["preview"].instance.setPixmapFromFile(PREVIEW_TMP_PATH)
		self["preview"].instance.setScale(1)

	def _downloadFailed(self, failure):
		print(f"[{PLUGIN_NAME}] preview download failed: {failure}")


class PiconSync:
	"""Syncs the PNGs published on a PiconBrowser GitHub Pages repo into a local directory."""

	def __init__(self, repoUrl, localPath, serviceList):
		self.repoUrl = repoUrl.rstrip("/")
		self.localPath = localPath
		self.serviceList = serviceList
		self.agent = BrowserLikeRedirectAgent(Agent(reactor))

	@inlineCallbacks
	def run(self):
		remoteMd5 = yield self._fetchMd5(f"{self.repoUrl}/info/files.md5")
		print(f"[{PLUGIN_NAME}] {len(remoteMd5)} entrie(s) in {self.repoUrl}/info/files.md5")
		remoteMap = yield self._fetchMap(f"{self.repoUrl}/info/files.map")
		print(f"[{PLUGIN_NAME}] {len(remoteMap)} entrie(s) in {self.repoUrl}/info/files.map")

		neededReal, neededAliases, missingChannels = self._determineNeededPicons(remoteMd5, remoteMap)
		print(f"[{PLUGIN_NAME}] {len(neededReal)} real picon(s), {len(neededAliases)} alias(es), {len(missingChannels)} missing")

		existing = {name for name in neededReal if exists(join(self.localPath, name))}
		missing = neededReal - existing
		print(f"[{PLUGIN_NAME}] downloading {len(missing)} missing picon(s)")
		for name in missing:
			yield self._downloadPicon(name)

		localMd5 = self._localMd5(existing)
		changed = {name for name in existing if localMd5.get(name) != remoteMd5.get(name)}
		for name in changed:
			yield self._downloadPicon(name)

		for target, source in neededAliases.items():
			yield self._createSymlink(source, target)

		self._writeMissingReport(missingChannels)

		return len(missing), len(changed), len(missingChannels)

	def _determineNeededPicons(self, remoteMd5, remoteMap):
		# The local picon is always saved under the service reference (sref) name, since
		# that's what enigma2 looks up. The actual file on git is named after the channel
		# name, mapped via files.map - or, if not listed there, named after the channel
		# name directly. files.md5 lists every real png that is actually available.
		neededAliases = {}
		missingChannels = []
		for service in self.serviceList:
			sref = getChannelKey(service)
			if not sref:
				continue
			serviceName = service.getServiceName().replace("\x80", "").replace("\x86", "").replace("\x87", "")
			if not serviceName.strip():
				continue
			cleanName = getCleanFileName(service.getServiceName())
			channelName = f"{cleanName}.png"
			realName = f"{remoteMap.get(cleanName, cleanName)}.png"
			if realName in remoteMd5:
				neededAliases[f"{sref}.png"] = realName
			else:
				missingChannels.append((serviceName, channelName, sref))

		neededReal = set(neededAliases.values())
		return neededReal, neededAliases, missingChannels

	def _writeMissingReport(self, missingChannels):
		# Same repo content is shared by every picon size/theme variant, so this list
		# only reflects what the source actually provides - not a flaw of this one repo.
		with open(join(self.localPath, "missing_picons.txt"), "w") as f:
			for serviceName, channelName, sref in sorted(set(missingChannels)):
				f.write(f"{serviceName};{channelName};{sref}\n")

	@inlineCallbacks
	def _fetchMd5(self, url):
		result = {}
		text = yield self._fetchText(url)
		for line in text.splitlines():
			parts = line.split(None, 1)
			if len(parts) == 2:
				checksum, name = parts
				result[name.lstrip("*")] = checksum
		return result

	@inlineCallbacks
	def _fetchMap(self, url):
		result = {}
		text = yield self._fetchText(url)
		for line in text.splitlines():
			parts = line.split("=", 1)
			if len(parts) == 2:
				target, source = parts
				result[target] = source
		return result

	@inlineCallbacks
	def _fetchText(self, url):
		response = yield self._request(url)
		body = yield readBody(response)
		return body.decode("utf-8")

	def _localMd5(self, names):
		result = {}
		for name in names:
			try:
				with open(join(self.localPath, name), "rb") as f:
					result[name] = md5(f.read()).hexdigest()
			except OSError:
				pass
		return result

	@inlineCallbacks
	def _downloadPicon(self, name):
		response = yield self._request(f"{self.repoUrl}/{name}")
		finished = Deferred()
		response.deliverBody(_PiconFileSaver(finished, join(self.localPath, name)))
		yield finished

	@inlineCallbacks
	def _createSymlink(self, source, target):
		sourcePath = join(self.localPath, source)
		targetPath = join(self.localPath, target)
		if not exists(sourcePath):
			yield self._downloadPicon(source)
		if islink(targetPath) or exists(targetPath):
			remove(targetPath)
		symlink(source, targetPath)

	@inlineCallbacks
	def _request(self, url):
		response = yield self.agent.request(b"GET", url.encode("utf-8"), Headers({"user-agent": ["PiconBrowser"]}))
		if response.code != 200:
			raise ValueError(f"GET {url} failed with status {response.code}")
		return response


class PiconBrowser:
	instance = None

	@classmethod
	def getInstance(cls, session):
		# SESSIONSTART is not repeated when the plugin is installed in a running GUI.
		if cls.instance is None:
			cls(session)
		return cls.instance

	def __init__(self, session):
		self.session = session
		self._runningSync = None
		from Scheduler import addFunctionTimer
		addFunctionTimer(SCHEDULER_TIMER_KEY, _("Picon Browser Sync"), self._schedulerRun, self._schedulerCancel, useOwnThread=True)
		PiconBrowser.instance = self

	def stop(self):
		self._schedulerCancel()

	def download(self):
		if self._runningSync and not self._runningSync.called:
			return None
		self._runningSync = self._syncAll()
		self._runningSync.addCallback(self._downloadDone).addErrback(self._downloadFailed)
		return self._runningSync

	def _schedulerRun(self, callback, timerEntry):
		deferred = self.download()
		if deferred:
			deferred.addCallbacks(lambda _: callback(True), lambda _: callback(False))
		else:
			callback(False)

	def _schedulerCancel(self):
		if self._runningSync and not self._runningSync.called:
			self._runningSync.cancel()

	@inlineCallbacks
	def _syncAll(self):
		results = []
		paths = getActivePiconPaths()
		print(f"[{PLUGIN_NAME}] sync starting for {len(paths)} path(s)")
		if not paths:
			return results
		serviceList = yield deferToThread(lambda: BouquetParser(BOUQUET_PATH).getServiceList())
		print(f"[{PLUGIN_NAME}] {len(serviceList)} channel(s) found in bouquets")
		for index, path in enumerate(paths):
			if not exists(path):
				print(f"[{PLUGIN_NAME}] skipping path, does not exist: {path}")
				continue
			repo = getattr(config.plugins.PiconBrowser, f"sets{index}").repo.value
			print(f"[{PLUGIN_NAME}] syncing repo '{repo}' into '{path}'")
			sync = PiconSync(REPOS[repo], path, serviceList)
			result = yield sync.run()
			results.append(result)
		return results

	def _downloadDone(self, results):
		for missing, changed, notFound in results:
			print(f"[{PLUGIN_NAME}] sync done: {missing} downloaded, {changed} updated, {notFound} not found")

	def _downloadFailed(self, failure):
		print(f"[{PLUGIN_NAME}] sync failed: {failure}")

	def openSetup(self):
		self.session.openWithCallback(self._setupClosed, PiconBrowserSetup)

	def _setupClosed(self):
		pass

	def shutdown(self):
		self.stop()
		from Scheduler import functionTimers
		functionTimers.remove(SCHEDULER_TIMER_KEY)
		PiconBrowser.instance = None


def autostart(reason, session=None, **kwargs):
	if reason == 0 and session is not None:
		PiconBrowser.getInstance(session)
	elif reason == 1:
		if PiconBrowser.instance:
			PiconBrowser.instance.shutdown()


def main(session, **kwargs):
	PiconBrowser.getInstance(session).openSetup()


def Plugins(**kwargs):
	return [
		PluginDescriptor(
			name=PLUGIN_NAME,
			description=_("PiconBrowser description"),
			icon=resolveFilename(SCOPE_CURRENT_PLUGIN, "Extensions/PiconBrowser/plugin.png"),
			where=PluginDescriptor.WHERE_PLUGINMENU,
			fnc=main,
		),
		PluginDescriptor(
			where=PluginDescriptor.WHERE_SESSIONSTART,
			fnc=autostart,
		),
	]
