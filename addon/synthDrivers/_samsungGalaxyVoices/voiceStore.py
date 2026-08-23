# license: GPL-2.0-or-later
"""Storage and Samsung catalogue support shared by the driver and settings panel."""

from collections import OrderedDict
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

import globalVars


DATA_ROOT = os.path.join(globalVars.appArgs.configPath, "samsungGalaxyVoices")
VOICES_DIR = os.path.join(DATA_ROOT, "voices")
ENGINES_DIR = os.path.join(DATA_ROOT, "engines")
SETTINGS_PATH = os.path.join(DATA_ROOT, "settings.json")
CATALOG_CACHE_PATH = os.path.join(DATA_ROOT, "catalog.json")
CATALOG_CACHE_MAX_AGE = 24 * 60 * 60
REQUIRED_ASSETS = {
	"assets/cfg": (1024, 1024 * 1024),
	"assets/lng": (1024 * 1024, 128 * 1024 * 1024),
}
MODEL_LIMITS = (1024 * 1024, 256 * 1024 * 1024)
ENGINE_MEMBER = "lib/arm64-v8a/libsamsungtts.so"
ENGINE_LIMITS = (1024 * 1024, 128 * 1024 * 1024)
MAX_PACKAGE_SIZE = 256 * 1024 * 1024
PREMIUM_CODES = (
	"en_us_g02", "en_us_l03", "en_us_l04", "en_us_l05",
	"ko_kr_g01", "ko_kr_l01", "ko_kr_l04", "ko_kr_l08",
	"en_gb_l02", "en_gb_g02", "en_in_l02",
	"es_es_l01", "es_es_g01", "fr_fr_l01", "fr_fr_g01",
	"de_de_l01", "de_de_g01", "it_it_l01", "it_it_g01",
	"pt_br_l01", "pt_br_g01", "zh_cn_l02", "zh_cn_g02",
	"es_us_l01", "es_us_g01",
)
COMPACT_CODES = (
	"cs_cz_f00", "da_dk_f00", "el_gr_f00", "en_au_f00", "en_au_m00",
	"en_in_f00", "es_mx_f00", "es_mx_m00", "es_us_f00", "fi_fi_f00",
	"fr_ca_f00", "hi_in_f00", "hu_hu_f00", "id_id_f00", "ja_jp_f00",
	"ja_jp_m00", "nb_no_f00", "nl_nl_f00", "pl_pl_f00", "pt_pt_f00",
	"ro_ro_f00", "ru_ru_f00", "ru_ru_m00", "sk_sk_f00", "sv_se_f00",
	"th_th_f00", "tr_tr_f00", "vi_vn_f00", "zh_cn_f00", "zh_cn_m00",
	"zh_hk_f00", "zh_tw_f00",
)
CATALOG_CODES = PREMIUM_CODES + COMPACT_CODES
# The Indian package has produced invalid audio with the compatible engine generation.
DOWNLOADABLE_CODES = tuple(code for code in CATALOG_CODES if code != "en_in_l02")
LANGUAGE_NAMES = {
	"cs_CZ": "Czech", "da_DK": "Danish", "de_DE": "German", "el_GR": "Greek",
	"en_AU": "Australian English", "en_GB": "UK English", "en_IN": "Indian English",
	"en_US": "US English", "es_ES": "European Spanish", "es_MX": "Mexican Spanish",
	"es_US": "US Spanish", "fi_FI": "Finnish", "fil_PH": "Filipino",
	"fr_CA": "Canadian French", "fr_FR": "French", "gu_IN": "Gujarati",
	"hi_IN": "Hindi", "hu_HU": "Hungarian", "id_ID": "Indonesian",
	"it_IT": "Italian", "ja_JP": "Japanese", "ko_KR": "Korean",
	"nb_NO": "Norwegian", "nl_NL": "Dutch", "pl_PL": "Polish",
	"pt_BR": "Brazilian Portuguese", "pt_PT": "European Portuguese",
	"ro_RO": "Romanian", "ru_RU": "Russian", "sk_SK": "Slovak",
	"sv_SE": "Swedish", "th_TH": "Thai", "tl_PH": "Tagalog",
	"tr_TR": "Turkish", "vi_VN": "Vietnamese", "zh_CN": "Simplified Chinese",
	"zh_HK": "Cantonese", "zh_TW": "Taiwanese Mandarin",
}
KNOWN_NAMES = {
	"en_GB_l02": "Amy Green",
	"en_GB_g02": "Chris",
	"en_US_l03": "Stephanie",
	"en_US_l04": "Julia",
	"en_US_l05": "Lisa",
}


def loadSettings():
	settings = {"updateInterval": "daily", "voicePromptShown": False, "playbackBufferMs": 0}
	try:
		with open(SETTINGS_PATH, "r", encoding="utf-8") as settingsFile:
			loaded = json.load(settingsFile)
	except (OSError, ValueError, TypeError):
		return settings
	if loaded.get("updateInterval") in {"never", "hourly", "daily"}:
		settings["updateInterval"] = loaded["updateInterval"]
	settings["voicePromptShown"] = bool(loaded.get("voicePromptShown", False))
	try:
		settings["playbackBufferMs"] = max(0, min(500, int(loaded.get("playbackBufferMs", 0))))
	except (TypeError, ValueError):
		pass
	return settings


def saveSettings(settings):
	os.makedirs(DATA_ROOT, exist_ok=True)
	temporaryPath = SETTINGS_PATH + ".tmp"
	with open(temporaryPath, "w", encoding="utf-8") as settingsFile:
		json.dump(settings, settingsFile, ensure_ascii=True, indent=2)
	os.replace(temporaryPath, SETTINGS_PATH)


def loadCatalogCache():
	try:
		with open(CATALOG_CACHE_PATH, "r", encoding="utf-8") as cacheFile:
			cache = json.load(cacheFile)
	except (OSError, ValueError, TypeError):
		return {}
	if not isinstance(cache, dict):
		return {}
	now = time.time()
	valid = {}
	for code, entry in cache.items():
		if not isinstance(entry, dict):
			continue
		try:
			size = int(entry["size"])
			checked = float(entry["checked"])
		except (KeyError, TypeError, ValueError):
			continue
		if 0 < size <= MAX_PACKAGE_SIZE and 0 <= now - checked <= CATALOG_CACHE_MAX_AGE:
			valid[code] = {"size": size, "checked": checked}
	return valid


def saveCatalogCache(cache):
	os.makedirs(DATA_ROOT, exist_ok=True)
	temporaryPath = CATALOG_CACHE_PATH + ".tmp"
	with open(temporaryPath, "w", encoding="utf-8") as cacheFile:
		json.dump(cache, cacheFile, ensure_ascii=True, indent=2)
	os.replace(temporaryPath, CATALOG_CACHE_PATH)


def voiceId(code):
	parts = code.split("_")
	return f"{parts[0]}_{parts[1].upper()}_{parts[2]}"


def languageId(code):
	parts = code.split("_")
	return f"{parts[0]}_{parts[1].upper()}"


def voicePath(code):
	return os.path.join(VOICES_DIR, code.replace("_", "-"))


def enginePath(engineHash):
	return os.path.join(ENGINES_DIR, engineHash, "libsamsungtts.so")


def readVoiceMetadata(code):
	try:
		with open(os.path.join(voicePath(code), "voice.json"), "r", encoding="utf-8") as metadataFile:
			return json.load(metadataFile)
	except (OSError, ValueError, TypeError):
		return None


def voiceLabel(code):
	metadata = readVoiceMetadata(code) or {}
	language = languageId(code)
	name = str(metadata.get("name") or KNOWN_NAMES.get(voiceId(code)) or "").strip()
	languageName = LANGUAGE_NAMES.get(language, language.replace("_", " "))
	if name:
		return f"{languageName} - {name}"
	family = code.rsplit("_", 1)[1]
	gender = "male" if family.startswith(("g", "m")) else "female"
	return f"{languageName}, {gender}, voice {family}"


def _modelName(code, metadata=None):
	metadata = metadata or {}
	model = str(metadata.get("model") or "")
	if model in {"regular.ivc", "tiny.ivc"}:
		return model
	return "tiny.ivc" if code.rsplit("_", 1)[1].startswith(("f", "m")) else "regular.ivc"


def isInstalled(code):
	metadata = readVoiceMetadata(code)
	if not isinstance(metadata, dict):
		return False
	engineHash = str(metadata.get("engineHash") or "")
	path = voicePath(code)
	return bool(re.fullmatch(r"[0-9a-f]{64}", engineHash)) and (
		os.path.isfile(enginePath(engineHash))
		and all(os.path.isfile(os.path.join(path, "assets", filename)) for filename in ("cfg", "lng", _modelName(code, metadata)))
	)


def loadVoiceDefinitions():
	definitions = []
	try:
		entries = os.scandir(VOICES_DIR)
	except OSError:
		return OrderedDict()
	with entries:
		for entry in entries:
			if not entry.is_dir(follow_symlinks=False):
				continue
			code = entry.name.replace("-", "_")
			metadata = readVoiceMetadata(code)
			if not metadata or not isInstalled(code):
				continue
			try:
				item = {
					"id": str(metadata["id"]),
					"name": str(metadata["name"]),
					"path": entry.path,
					"enginePath": enginePath(str(metadata["engineHash"])),
					"family": str(metadata["family"]),
					"speaker": int(metadata["speaker"]),
					"language": str(metadata["language"]),
				}
			except (KeyError, TypeError, ValueError):
				continue
			if item["family"] not in {"f", "g", "l", "m"} or not 0 <= item["speaker"] <= 99:
				continue
			definitions.append(item)
	definitions.sort(key=lambda item: item["name"].casefold())
	return OrderedDict((item.pop("id"), item) for item in definitions)


def _isSamsungHost(url):
	host = (urllib.parse.urlparse(url).hostname or "").lower()
	return host == "samsungapps.com" or host.endswith(".samsungapps.com")


def downloadMetadata(code):
	packageName = f"com.samsung.SMT.lang_{code}"
	params = urllib.parse.urlencode({
		"appId": packageName, "deviceId": "SM-G970F", "mcc": "234", "mnc": "15",
		"csc": "BTU", "sdkVer": "29", "pd": "0", "systemId": "0",
		"callerId": "com.sec.android.app.samsungapps", "abiType": "64",
		"extuk": "0000000000000000",
	})
	url = f"https://vas.samsungapps.com/stub/stubDownload.as?{params}"
	request = urllib.request.Request(url, headers={"User-Agent": "SamsungGalaxyVoices/1.0"})
	with urllib.request.urlopen(request, timeout=20) as response:
		if not _isSamsungHost(response.geturl()):
			raise RuntimeError("Samsung redirected the catalogue request to an unexpected server.")
		root = ET.fromstring(response.read(256 * 1024))
	if root.findtext("resultCode") != "1":
		raise RuntimeError(root.findtext("resultMsg") or "Samsung did not provide this voice.")
	downloadUrl = root.findtext("downloadURI") or ""
	if not _isSamsungHost(downloadUrl):
		raise RuntimeError("Samsung returned an unexpected download server.")
	size = int(root.findtext("contentSize") or 0)
	if not 1024 * 1024 <= size <= MAX_PACKAGE_SIZE:
		raise RuntimeError("Samsung returned an invalid package size.")
	return {
		"url": downloadUrl, "size": size, "version": root.findtext("versionCode") or "",
		"productName": root.findtext("productName") or voiceLabel(code), "package": packageName,
	}


def _downloadPackage(metadata, destination, progress):
	request = urllib.request.Request(metadata["url"], headers={"User-Agent": "SamsungGalaxyVoices/1.0"})
	with urllib.request.urlopen(request, timeout=30) as response, open(destination, "wb") as output:
		if not _isSamsungHost(response.geturl()):
			raise RuntimeError("Samsung redirected the download to an unexpected server.")
		total = metadata["size"]
		written = 0
		while True:
			chunk = response.read(1024 * 1024)
			if not chunk:
				break
			output.write(chunk)
			written += len(chunk)
			if written > total:
				raise RuntimeError("The downloaded voice is larger than Samsung declared.")
			progress(written, total)
	if written != metadata["size"]:
		raise RuntimeError("The Samsung voice download was incomplete.")


def _cfgValues(path):
	with open(path, "rb") as cfgFile:
		parts = cfgFile.read().split(b"\0")
	values = []
	for part in parts:
		try:
			text = part.decode("utf-8").strip()
		except UnicodeDecodeError:
			continue
		if text and all(character.isprintable() for character in text):
			values.append(text)
	return values


def _cfgValue(values, key):
	for index in range(len(values) - 1):
		if values[index] == key:
			return values[index + 1]
	return ""


def _validateEngine(path):
	with open(path, "rb") as engineFile:
		header = engineFile.read(64)
	if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 183:
		raise RuntimeError("The Samsung package does not contain a valid ARM64 speech engine.")


_installLock = threading.Lock()


def _installVoice(code, progress):
	metadata = downloadMetadata(code)
	os.makedirs(DATA_ROOT, exist_ok=True)
	workDir = tempfile.mkdtemp(prefix="voice-", dir=DATA_ROOT)
	packagePath = os.path.join(workDir, "voice.apk")
	stagingPath = os.path.join(workDir, "staging")
	assetsPath = os.path.join(stagingPath, "assets")
	os.makedirs(assetsPath)
	try:
		_downloadPackage(metadata, packagePath, progress)
		engineTemp = os.path.join(workDir, "libsamsungtts.so")
		modelName = "tiny.ivc" if code in COMPACT_CODES else "regular.ivc"
		with zipfile.ZipFile(packagePath, "r") as package:
			for asset, limits in REQUIRED_ASSETS.items():
				try:
					info = package.getinfo(asset)
				except KeyError as error:
					raise RuntimeError("The Samsung package does not contain the expected regular voice data.") from error
				if not limits[0] <= info.file_size <= limits[1]:
					raise RuntimeError("A voice data file has an invalid size.")
				with package.open(info) as source, open(os.path.join(assetsPath, os.path.basename(asset)), "wb") as output:
					shutil.copyfileobj(source, output, length=1024 * 1024)
			modelMember = f"assets/{modelName}"
			try:
				modelInfo = package.getinfo(modelMember)
			except KeyError as error:
				raise RuntimeError("The Samsung package does not contain the expected voice model.") from error
			if not MODEL_LIMITS[0] <= modelInfo.file_size <= MODEL_LIMITS[1]:
				raise RuntimeError("A voice model file has an invalid size.")
			with package.open(modelInfo) as source, open(os.path.join(assetsPath, modelName), "wb") as output:
				shutil.copyfileobj(source, output, length=1024 * 1024)
			try:
				engineInfo = package.getinfo(ENGINE_MEMBER)
			except KeyError as error:
				raise RuntimeError("The Samsung package does not contain its ARM64 speech engine.") from error
			if not ENGINE_LIMITS[0] <= engineInfo.file_size <= ENGINE_LIMITS[1]:
				raise RuntimeError("The Samsung speech engine has an invalid size.")
			with package.open(engineInfo) as source, open(engineTemp, "wb") as output:
				shutil.copyfileobj(source, output, length=1024 * 1024)
		_validateEngine(engineTemp)
		with open(engineTemp, "rb") as engineFile:
			engineHash = hashlib.sha256(engineFile.read()).hexdigest()
		engineDir = os.path.join(ENGINES_DIR, engineHash)
		os.makedirs(engineDir, exist_ok=True)
		finalEngine = os.path.join(engineDir, "libsamsungtts.so")
		if not os.path.isfile(finalEngine):
			os.replace(engineTemp, finalEngine)
		values = _cfgValues(os.path.join(assetsPath, "cfg"))
		parts = code.split("_")
		name = _cfgValue(values, "name") or KNOWN_NAMES.get(voiceId(code)) or metadata["productName"]
		voiceMetadata = {
			"id": voiceId(code), "language": languageId(code), "family": parts[2][0],
			"speaker": int(parts[2][1:]), "name": name,
			"productName": metadata["productName"], "package": metadata["package"],
			"version": metadata["version"], "engineHash": engineHash, "model": modelName,
		}
		with open(os.path.join(stagingPath, "voice.json"), "w", encoding="utf-8") as metadataFile:
			json.dump(voiceMetadata, metadataFile, ensure_ascii=True, indent=2)
		targetPath = voicePath(code)
		os.makedirs(VOICES_DIR, exist_ok=True)
		backupPath = targetPath + ".old"
		if os.path.isdir(backupPath):
			shutil.rmtree(backupPath)
		if os.path.isdir(targetPath):
			os.replace(targetPath, backupPath)
		try:
			os.replace(stagingPath, targetPath)
		except Exception:
			if os.path.isdir(backupPath):
				os.replace(backupPath, targetPath)
			raise
		shutil.rmtree(backupPath, ignore_errors=True)
		return name
	finally:
		shutil.rmtree(workDir, ignore_errors=True)


def installVoice(code, progress):
	if not _installLock.acquire(blocking=False):
		raise RuntimeError("Another Samsung Galaxy voice installation is already in progress.")
	try:
		return _installVoice(code, progress)
	finally:
		_installLock.release()


def removeVoices(codes):
	for code in codes:
		shutil.rmtree(voicePath(code))
	garbageCollectEngines()


def garbageCollectEngines():
	referenced = set()
	try:
		with os.scandir(VOICES_DIR) as entries:
			for entry in entries:
				if not entry.is_dir(follow_symlinks=False):
					continue
				metadata = readVoiceMetadata(entry.name.replace("-", "_")) or {}
				engineHash = str(metadata.get("engineHash") or "")
				if re.fullmatch(r"[0-9a-f]{64}", engineHash):
					referenced.add(engineHash)
	except OSError:
		pass
	try:
		engineEntries = os.scandir(ENGINES_DIR)
	except OSError:
		return
	with engineEntries:
		for entry in engineEntries:
			if entry.is_dir(follow_symlinks=False) and entry.name not in referenced:
				shutil.rmtree(entry.path, ignore_errors=True)
