# license: GPL-2.0-or-later
"""Preserve compatible voices and engines when upgrading earlier builds."""

import hashlib
import json
import os
import re
import shutil

import addonHandler
import globalVars
from logHandler import log


ADDON_NAME = "samsungGalaxyVoices"
DATA_ROOT = os.path.join(globalVars.appArgs.configPath, ADDON_NAME)
VOICES_DIR = os.path.join(DATA_ROOT, "voices")
ENGINES_DIR = os.path.join(DATA_ROOT, "engines")
GENERATIONS = frozenset({"legacy", "s24"})
KNOWN_BAD_PACKAGES = frozenset({
	("en_in_l02", "legacy", "312347000"),
	("en_in_l02", "s24", "312501000"),
	("en_us_g02", "s24", "312504000"),
	("en_us_l03", "s24", "312504000"),
})
BUNDLED_VOICES = {
	"en-gb-l02": {
		"id": "en_GB_l02", "language": "en_GB", "family": "l", "speaker": 2,
		"name": "Amy Green", "productName": "Samsung TTS UK English Voice 1",
		"package": "com.samsung.SMT.lang_en_gb_l02", "version": "312347000",
	},
	"en-gb-g02": {
		"id": "en_GB_g02", "language": "en_GB", "family": "g", "speaker": 2,
		"name": "Chris", "productName": "Samsung TTS UK English Voice 2",
		"package": "com.samsung.SMT.lang_en_gb_g02", "version": "312347000",
	},
}


def _hashFile(path):
	digest = hashlib.sha256()
	with open(path, "rb") as source:
		while True:
			block = source.read(1024 * 1024)
			if not block:
				break
			digest.update(block)
	return digest.hexdigest()


def _embeddedName(cfgPath):
	try:
		with open(cfgPath, "rb") as cfgFile:
			parts = cfgFile.read().split(b"\0")
	except OSError:
		return ""
	values = []
	for part in parts:
		try:
			text = part.decode("utf-8").strip()
		except UnicodeDecodeError:
			continue
		if text and all(character.isprintable() for character in text):
			values.append(text)
	for index in range(len(values) - 1):
		if values[index] == "name":
			return values[index + 1]
	return ""


def _legacyAddonPath():
	for addon in addonHandler.getAvailableAddons():
		if addon.name == ADDON_NAME and os.path.isdir(addon.path):
			return addon.path
	return None


def _validateVoice(path, metadata, enginePath):
	engineHash = str(metadata.get("engineHash") or "")
	model = str(metadata.get("model") or "regular.ivc")
	if model not in {"regular.ivc", "tiny.ivc"}:
		return False
	return bool(re.fullmatch(r"[0-9a-f]{64}", engineHash)) and os.path.isfile(enginePath) and all(
		os.path.isfile(os.path.join(path, "assets", filename))
		for filename in ("cfg", "lng", model)
	)


def _voiceDirectories():
	try:
		rootEntries = list(os.scandir(VOICES_DIR))
	except OSError:
		return
	for entry in rootEntries:
		if not entry.is_dir(follow_symlinks=False):
			continue
		if entry.name not in GENERATIONS:
			yield entry.path
			continue
		try:
			generationEntries = os.scandir(entry.path)
		except OSError:
			continue
		with generationEntries:
			for voiceEntry in generationEntries:
				if voiceEntry.is_dir(follow_symlinks=False):
					yield voiceEntry.path


def _catalogKey(metadata):
	generation = str(metadata.get("generation") or "legacy")
	key = str(metadata.get("catalogKey") or "").lower()
	if key:
		return key
	identifier = str(metadata.get("id") or "").lower()
	if generation != "legacy" and identifier.endswith(f"_{generation}"):
		identifier = identifier[:-(len(generation) + 1)]
	return identifier if generation == "legacy" else f"{identifier}@{generation}"


def _isBlockedPackage(metadata):
	generation = str(metadata.get("generation") or "legacy")
	key = _catalogKey(metadata)
	if "@" in key:
		code, keyGeneration = key.rsplit("@", 1)
	else:
		code, keyGeneration = key, generation
	return (code, keyGeneration, str(metadata.get("version") or "")) in KNOWN_BAD_PACKAGES


def _removeBlockedVoicePackages():
	removed = 0
	for voiceDirectory in tuple(_voiceDirectories()):
		try:
			with open(os.path.join(voiceDirectory, "voice.json"), "r", encoding="utf-8") as metadataFile:
				metadata = json.load(metadataFile)
		except (OSError, ValueError, TypeError):
			continue
		if not _isBlockedPackage(metadata):
			continue
		try:
			shutil.rmtree(voiceDirectory)
		except OSError:
			# A running old host may still hold a voice open. Runtime maintenance retries after restart.
			continue
		removed += 1
	return removed


def _garbageCollectEngines():
	referenced = set()
	for voiceDirectory in _voiceDirectories():
		try:
			with open(os.path.join(voiceDirectory, "voice.json"), "r", encoding="utf-8") as metadataFile:
				metadata = json.load(metadataFile)
		except (OSError, ValueError, TypeError):
			continue
		engineHash = str(metadata.get("engineHash") or "")
		if re.fullmatch(r"[0-9a-f]{64}", engineHash):
			referenced.add(engineHash)
	try:
		engineEntries = os.scandir(ENGINES_DIR)
	except OSError:
		return
	with engineEntries:
		for entry in engineEntries:
			if entry.is_dir(follow_symlinks=False) and entry.name not in referenced:
				shutil.rmtree(entry.path, ignore_errors=True)


def _migrateVoiceLayout():
	"""Move validated flat voice folders into generation-specific directories."""
	migrated = 0
	try:
		rootEntries = list(os.scandir(VOICES_DIR))
	except OSError:
		return migrated
	for entry in rootEntries:
		if not entry.is_dir(follow_symlinks=False) or entry.name in GENERATIONS:
			continue
		metadataPath = os.path.join(entry.path, "voice.json")
		try:
			with open(metadataPath, "r", encoding="utf-8") as metadataFile:
				metadata = json.load(metadataFile)
		except (OSError, ValueError, TypeError):
			continue
		generation = str(metadata.get("generation") or "legacy")
		if generation not in GENERATIONS:
			continue
		folder = entry.name.removesuffix(f"--{generation}")
		target = os.path.join(VOICES_DIR, generation, folder)
		enginePath = _existingEnginePath(metadata)
		if enginePath is None or not _validateVoice(entry.path, metadata, enginePath):
			continue
		if os.path.exists(target):
			log.info("Samsung Galaxy Voices: retained duplicate voice folder %s", entry.path)
			continue
		try:
			os.makedirs(os.path.dirname(target), exist_ok=True)
			os.replace(entry.path, target)
		except OSError:
			# Retry from the new add-on before its first host starts.
			continue
		migrated += 1
	return migrated


def _existingEnginePath(metadata):
	engineHash = str(metadata.get("engineHash") or "")
	if not re.fullmatch(r"[0-9a-f]{64}", engineHash):
		return None
	path = os.path.join(ENGINES_DIR, engineHash, "libsamsungtts.so")
	return path if os.path.isfile(path) else None


def onInstall():
	removedVoices = _removeBlockedVoicePackages()
	if removedVoices:
		_garbageCollectEngines()
		log.info("Samsung Galaxy Voices: removed %d incompatible voice package(s)", removedVoices)
	migratedVoices = _migrateVoiceLayout()
	legacyPath = _legacyAddonPath()
	if not legacyPath:
		if migratedVoices:
			log.info("Samsung Galaxy Voices: migrated %d voice folder(s) to the generation layout", migratedVoices)
		return
	legacyData = os.path.join(legacyPath, "synthDrivers", "_samsungGalaxyVoices")
	legacyEngine = os.path.join(legacyData, "engines", "regular", "libsamsungtts.so")
	if not os.path.isfile(legacyEngine):
		if migratedVoices:
			log.info("Samsung Galaxy Voices: migrated %d voice folder(s) to the generation layout", migratedVoices)
		return
	engineHash = _hashFile(legacyEngine)
	engineDir = os.path.join(ENGINES_DIR, engineHash)
	enginePath = os.path.join(engineDir, "libsamsungtts.so")
	createdEngine = False
	createdVoices = []
	metadataBackups = {}
	try:
		os.makedirs(engineDir, exist_ok=True)
		if not os.path.isfile(enginePath):
			temporaryEngine = enginePath + ".tmp"
			shutil.copy2(legacyEngine, temporaryEngine)
			if _hashFile(temporaryEngine) != engineHash:
				raise RuntimeError("The legacy Samsung engine copy did not verify.")
			os.replace(temporaryEngine, enginePath)
			createdEngine = True
		os.makedirs(VOICES_DIR, exist_ok=True)
		legacyVoices = os.path.join(legacyData, "voices")
		for folder, defaults in BUNDLED_VOICES.items():
			source = os.path.join(legacyVoices, folder)
			target = os.path.join(VOICES_DIR, "legacy", folder)
			if not os.path.isdir(source) or os.path.exists(target):
				continue
			temporaryTarget = target + ".migration"
			shutil.rmtree(temporaryTarget, ignore_errors=True)
			shutil.copytree(source, temporaryTarget)
			metadata = dict(defaults)
			metadata["engineHash"] = engineHash
			with open(os.path.join(temporaryTarget, "voice.json"), "w", encoding="utf-8") as metadataFile:
				json.dump(metadata, metadataFile, ensure_ascii=True, indent=2)
			if not _validateVoice(temporaryTarget, metadata, enginePath):
				raise RuntimeError(f"The legacy Samsung voice {folder} did not validate.")
			os.replace(temporaryTarget, target)
			createdVoices.append(target)
		for voiceDirectory in _voiceDirectories():
			metadataPath = os.path.join(voiceDirectory, "voice.json")
			if not os.path.isfile(metadataPath):
				continue
			with open(metadataPath, "rb") as metadataFile:
				original = metadataFile.read()
			metadata = json.loads(original.decode("utf-8"))
			currentEnginePath = _existingEnginePath(metadata)
			if currentEnginePath is None:
				metadata["engineHash"] = engineHash
				currentEnginePath = enginePath
			name = _embeddedName(os.path.join(voiceDirectory, "assets", "cfg"))
			if name:
				metadata["name"] = name
			metadata.setdefault("productName", metadata.get("name", ""))
			updated = json.dumps(metadata, ensure_ascii=True, indent=2).encode("utf-8")
			if updated == original:
				continue
			metadataBackups[metadataPath] = original
			temporaryMetadata = metadataPath + ".tmp"
			with open(temporaryMetadata, "wb") as metadataFile:
				metadataFile.write(updated)
			os.replace(temporaryMetadata, metadataPath)
			if not _validateVoice(voiceDirectory, metadata, currentEnginePath):
				raise RuntimeError(f"The migrated Samsung voice {os.path.basename(voiceDirectory)} did not validate.")
		migratedVoices += _migrateVoiceLayout()
		log.info("Samsung Galaxy Voices: preserved %d voice(s) for the voice-free upgrade", len(metadataBackups))
		if migratedVoices:
			log.info("Samsung Galaxy Voices: migrated %d voice folder(s) to the generation layout", migratedVoices)
	except Exception:
		for metadataPath, original in metadataBackups.items():
			try:
				with open(metadataPath, "wb") as metadataFile:
					metadataFile.write(original)
			except OSError:
				pass
		for path in createdVoices:
			shutil.rmtree(path, ignore_errors=True)
		if createdEngine:
			try:
				os.remove(enginePath)
			except OSError:
				pass
		raise
