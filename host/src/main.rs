use std::cell::RefCell;
use std::collections::HashMap;
use std::env;
use std::fs::{self, File};
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};

#[no_mangle]
pub extern "C" fn getpagesize() -> i32 {
    4096
}
use std::rc::Rc;
use std::sync::{Arc, Mutex, mpsc};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;

use anyhow::{bail, Context, Result};
use emulator::android::virtual_library::libc::Libc;
use emulator::linux::file_system::{FileIO, StMode};
use emulator::linux::fs::ByteArrayFileIO;
use emulator::linux::fs::linux_file::LinuxFileIO;
use emulator::memory::svc_memory::{Arm64Svc, SvcCallResult};
use emulator::{AndroidEmulator, RegisterARM64, UnicornArg};

const LEGACY_CANCEL_ENGINE_VERSION_MAX: u64 = 499_999_999;
#[derive(Clone, Copy)]
struct CancelLayout {
    synthesizer_offset: u64,
    stop_requested_offset: u64,
    audio_port_offset: u64,
    audio_port_stop_offset: u64,
}

const LEGACY_CANCEL_LAYOUT: CancelLayout = CancelLayout {
    synthesizer_offset: 0x2da0,
    stop_requested_offset: 0x8357f4,
    audio_port_offset: 0x657b8,
    audio_port_stop_offset: 0x39,
};

const S24_CANCEL_LAYOUT: CancelLayout = CancelLayout {
    synthesizer_offset: 0x2da8,
    stop_requested_offset: 0x8365d4,
    audio_port_offset: 0x66598,
    audio_port_stop_offset: 0x59,
};

fn request_samsung_stop(
    emulator: &AndroidEmulator<()>,
    engine: u64,
    layout: CancelLayout,
) -> Result<()> {
    let synthesizer = emulator.backend.mem_read_u64(engine + layout.synthesizer_offset)?;
    if synthesizer == 0 {
        bail!("Samsung synthesizer is unavailable");
    }
    emulator.backend.mem_write(
        synthesizer + layout.stop_requested_offset,
        &1u32.to_le_bytes(),
    )?;
    let audio_port = emulator.backend.mem_read_u64(synthesizer + layout.audio_port_offset)?;
    if audio_port == 0 {
        bail!("Samsung audio port is unavailable");
    }
    emulator.backend.mem_write(
        audio_port + layout.audio_port_stop_offset,
        &[0],
    )?;
    Ok(())
}

struct AudioCallback {
    pcm: Rc<RefCell<Vec<u8>>>,
    produced_bytes: Arc<AtomicUsize>,
    cancel_after: Arc<AtomicUsize>,
    cancel_requested: Arc<AtomicBool>,
    cancelled: Arc<AtomicBool>,
    stream: Option<Arc<Mutex<BufWriter<std::io::Stdout>>>>,
    engine: u64,
    cancel_layout: CancelLayout,
}

impl Arm64Svc<()> for AudioCallback {
    fn name(&self) -> &str { "SamsungAudioCallback" }

    fn handle(&self, emulator: &AndroidEmulator<()>) -> SvcCallResult {
        if self.cancel_requested.load(Ordering::Acquire) {
            self.cancelled.store(true, Ordering::Release);
            if let Err(error) = request_samsung_stop(emulator, self.engine, self.cancel_layout) {
                return SvcCallResult::FUCK(error);
            }
            return SvcCallResult::RET(0);
        }
        let data = match emulator.backend.reg_read(RegisterARM64::X0) {
            Ok(value) => value,
            Err(error) => return SvcCallResult::FUCK(error.into()),
        };
        let size = match emulator.backend.reg_read(RegisterARM64::X1) {
            Ok(value) => value as usize,
            Err(error) => return SvcCallResult::FUCK(error.into()),
        };
        if data != 0 && size != 0 {
            match emulator.backend.mem_read_as_vec(data, size) {
                Ok(bytes) => {
                    self.produced_bytes.fetch_add(bytes.len(), Ordering::Relaxed);
                    if self.stream.is_none() {
                        self.pcm.borrow_mut().extend_from_slice(&bytes);
                    }
                    if let Some(stream) = &self.stream {
                        let result = stream.lock()
                            .map_err(|_| anyhow::anyhow!("Samsung host output lock was poisoned"))
                            .and_then(|mut output| write_frame(&mut *output, b'A', &bytes));
                        if let Err(error) = result {
                            return SvcCallResult::FUCK(error.into());
                        }
                    }
                }
                Err(error) => return SvcCallResult::FUCK(error.into()),
            }
        }
        let cancel_after = self.cancel_after.load(Ordering::Relaxed);
        if cancel_after > 0 && self.produced_bytes.load(Ordering::Relaxed) >= cancel_after {
            self.cancel_after.store(0, Ordering::Relaxed);
            self.cancelled.store(true, Ordering::Release);
            emulator.request_stop();
        }
        SvcCallResult::RET(0)
    }
}

enum HostCommand {
    Speak(String),
    Parameters(i32, i32),
    Stop,
    Quit,
    ReaderError(String),
}

fn write_frame(output: &mut impl Write, kind: u8, payload: &[u8]) -> Result<()> {
    let length = u32::try_from(payload.len()).context("host frame is too large")?;
    output.write_all(&[kind])?;
    output.write_all(&length.to_le_bytes())?;
    output.write_all(payload)?;
    output.flush()?;
    Ok(())
}

fn read_command(input: &mut impl Read) -> Result<Option<HostCommand>> {
    let mut kind = [0u8; 1];
    match input.read_exact(&mut kind) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    let mut length = [0u8; 4];
    input.read_exact(&mut length)?;
    let length = u32::from_le_bytes(length) as usize;
    if length > 16 * 1024 * 1024 {
        bail!("host command exceeds the 16 MB limit");
    }
    let mut payload = vec![0u8; length];
    input.read_exact(&mut payload)?;
    match kind[0] {
        b'S' => Ok(Some(HostCommand::Speak(String::from_utf8(payload)
            .context("speak command is not valid UTF-8")?))),
        b'P' if payload.len() == 8 => Ok(Some(HostCommand::Parameters(
            i32::from_le_bytes(payload[0..4].try_into().unwrap()),
            i32::from_le_bytes(payload[4..8].try_into().unwrap()),
        ))),
        b'X' if payload.is_empty() => Ok(Some(HostCommand::Stop)),
        b'Q' if payload.is_empty() => Ok(Some(HostCommand::Quit)),
        other => bail!("unknown or malformed host command: 0x{other:02x}"),
    }
}

fn call(
    module: &emulator::linux::module::LinuxModule<()>,
    emulator: &AndroidEmulator<()>,
    name: &str,
    args: Vec<UnicornArg>,
) -> Result<u64> {
    let symbol = module.find_symbol_by_name(name, true)
        .with_context(|| format!("{name} export not found"))?;
    symbol.call(emulator, args)
        .with_context(|| format!("{name} call failed"))
}

fn write_wave(path: &Path, sample_rate: u32, pcm: &[u8]) -> Result<()> {
    let mut file = File::create(path)
        .with_context(|| format!("could not create {}", path.display()))?;
    let data_size = u32::try_from(pcm.len()).context("audio is too large for WAV")?;
    let riff_size = 36u32.checked_add(data_size).context("WAV size overflow")?;
    file.write_all(b"RIFF")?;
    file.write_all(&riff_size.to_le_bytes())?;
    file.write_all(b"WAVEfmt ")?;
    file.write_all(&16u32.to_le_bytes())?;
    file.write_all(&1u16.to_le_bytes())?;
    file.write_all(&1u16.to_le_bytes())?;
    file.write_all(&sample_rate.to_le_bytes())?;
    file.write_all(&(sample_rate * 2).to_le_bytes())?;
    file.write_all(&2u16.to_le_bytes())?;
    file.write_all(&16u16.to_le_bytes())?;
    file.write_all(b"data")?;
    file.write_all(&data_size.to_le_bytes())?;
    file.write_all(pcm)?;
    file.flush()?;
    Ok(())
}

fn main() -> Result<()> {
    env_logger::init();
    let usage = "usage: rnidbg [--server] LIBSAMSUNGTTS_SO VOICE_DIR RNIDBG_ANDROID_SDK [FAMILY SPEAKER | OUTPUT.wav [TEXT]]";
    let mut args = env::args().skip(1);
    let first = args.next().context(usage)?;
    let server_mode = first == "--server";
    let library_path = if server_mode { args.next().context(usage)? } else { first };
    let voice_dir = PathBuf::from(args.next().context(usage)?);
    let sdk_path = args.next().context(usage)?;
    let voice_family = if server_mode { args.next().unwrap_or_else(|| "F".to_string()) } else { "F".to_string() };
    let voice_speaker = if server_mode {
        args.next().unwrap_or_else(|| "0".to_string()).parse::<u32>()
            .context("voice speaker must be an unsigned integer")?
    } else { 0 };
    let output_path = if server_mode { None } else { Some(PathBuf::from(args.next().context(usage)?)) };
    let text = if server_mode { None } else { Some(args.next().unwrap_or_else(||
        "Samsung Galaxy speech is running locally on Windows.".to_string())) };

    if !Path::new(&library_path).is_file() { bail!("Samsung library not found: {library_path}"); }
    if !voice_dir.is_dir() { bail!("Samsung voice folder not found: {}", voice_dir.display()); }
    if !Path::new(&sdk_path).is_dir() { bail!("RNIDBG Android SDK not found: {sdk_path}"); }

    let model_name = if voice_dir.join("assets/tiny.ivc").is_file() {
        "tiny.ivc"
    } else if voice_dir.join("assets/regular.ivc").is_file() {
        "regular.ivc"
    } else {
        bail!("Samsung voice model not found in {}", voice_dir.join("assets").display());
    };
    let voice_files = vec![
        ("/voice/current/assets/cfg".to_string(), voice_dir.join("assets/cfg"), 4i32),
        ("/voice/current/assets/lng".to_string(), voice_dir.join("assets/lng"), 5i32),
        (format!("/voice/current/assets/{model_name}"), voice_dir.join(format!("assets/{model_name}")), 8i32),
    ];
    for (_, path, _) in &voice_files {
        if !path.is_file() { bail!("required voice asset is missing: {}", path.display()); }
    }

    env::set_var("BASE_PATH", &sdk_path);
    let emulator = AndroidEmulator::create_arm64(42000, 1, "com.samsung.SMT", ());
    let mut libc = Libc::new();
    libc.set_system_property_service(Rc::new(Box::new(|name| match name {
        "ro.build.version.sdk" => Some("23".to_string()),
        "ro.product.cpu.abi" | "ro.product.cpu.abilist" => Some("arm64-v8a".to_string()),
        "ro.product.manufacturer" | "ro.product.brand" => Some("samsung".to_string()),
        "ro.product.model" => Some("SM-G970F".to_string()),
        _ => Some(String::new()),
    })));
    emulator.memory().add_hook_listeners(Box::new(libc));

    let vm = emulator.get_dalvik_vm();
    let module_cell = vm.load_library(emulator.clone(), &library_path, true)
        .context("failed to load Samsung ARM64 library")?;
    let module = unsafe { &*module_cell.get() };
    eprintln!("STAGE=library-loaded");
    let version = call(module, &emulator, "SMTGetVersion", vec![])?;
    let version_string = call(module, &emulator, "SMTGetVersion_String", vec![])?;
    if version_string != 0 {
        let _ = emulator.backend.mem_read_c_string(version_string)?;
    }
    let engine = call(module, &emulator, "TTS_ENGINE_New", vec![])?;
    if engine == 0 { bail!("Samsung returned a null TTS engine pointer"); }

    let redirects: HashMap<String, PathBuf> = voice_files.iter()
        .map(|(guest, host, _)| (guest.clone(), host.clone())).collect();
    emulator.get_file_system().set_file_resolver(Box::new(move |_, path, flags, _| {
        redirects.get(path).map(|host| FileIO::File(LinuxFileIO::new(
            host.to_string_lossy().as_ref(), path, flags.bits(), 0, StMode::SYSTEM_FILE,
        )))
    }));

    for path in ["/dev/stdin", "/dev/stdout", "/dev/stderr"] {
        let fd = emulator.get_file_system().insert_file(FileIO::Bytes(ByteArrayFileIO::new(
            Vec::new(), path.to_string(), 0, 0, StMode::SYSTEM_FILE,
        )));
        if fd > 2 {
            bail!("could not reserve Android standard descriptor {fd} for {path}");
        }
    }
    let open = module.find_symbol_by_name("open", true).context("Android open export not found")?;
    let voice_info = emulator.falloc(voice_files.len() * 32, false)?;
    for (index, (guest_path, host_path, kind)) in voice_files.iter().enumerate() {
        let guest_name = emulator.falloc(guest_path.len() + 1, false)?;
        guest_name.write_c_string(guest_path)?;
        let fd = open.call(&emulator, vec![
            UnicornArg::Ptr(guest_name.addr), UnicornArg::I32(0), UnicornArg::I32(0),
        ]).context("Android open call failed")? as i32;
        eprintln!("STAGE=file-opened path={guest_path} fd={fd}");
        if fd < 0 { bail!("Samsung voice asset could not be opened: {}", host_path.display()); }
        let offset = (index * 32) as u64;
        voice_info.write_u64_with_offset(offset, guest_name.addr)?;
        voice_info.write_i32_with_offset(offset + 8, *kind)?;
        voice_info.write_i32_with_offset(offset + 12, fd)?;
        voice_info.write_u64_with_offset(offset + 16, 0)?;
        voice_info.write_u64_with_offset(offset + 24, fs::metadata(host_path)?.len())?;
    }
    let set_voice_result = call(module, &emulator, "TTS_ENGINE_SetVoiceWithFD", vec![
        UnicornArg::Ptr(engine), UnicornArg::I32(3), UnicornArg::Ptr(voice_info.addr),
        UnicornArg::I32(3), UnicornArg::Str("eng".to_string()),
        UnicornArg::Str("GBR".to_string()), UnicornArg::Str(voice_family),
        UnicornArg::U32(voice_speaker),
    ])? as i32;
    eprintln!("STAGE=voice-set result={set_voice_result}");
    if set_voice_result != 0 { bail!("TTS_ENGINE_SetVoiceWithFD returned {set_voice_result}"); }

    let pcm = Rc::new(RefCell::new(Vec::new()));
    let produced_bytes = Arc::new(AtomicUsize::new(0));
    let cancel_after = Arc::new(AtomicUsize::new(env::var("SAMSUNG_CANCEL_AFTER_BYTES")
        .ok().and_then(|value| value.parse().ok()).unwrap_or(0)));
    let cancel_requested = Arc::new(AtomicBool::new(false));
    let cancelled = Arc::new(AtomicBool::new(false));
    let stream = server_mode.then(|| Arc::new(Mutex::new(BufWriter::new(std::io::stdout()))));
    let callback = emulator.register_svc(Box::new(AudioCallback {
        pcm: pcm.clone(),
        produced_bytes: produced_bytes.clone(),
        cancel_after: cancel_after.clone(),
        cancel_requested: cancel_requested.clone(),
        cancelled: cancelled.clone(),
        stream: stream.clone(),
        engine,
        cancel_layout: if version <= LEGACY_CANCEL_ENGINE_VERSION_MAX {
            LEGACY_CANCEL_LAYOUT
        } else {
            S24_CANCEL_LAYOUT
        },
    }));
    call(module, &emulator, "TTS_ENGINE_SetAudioCallback", vec![
        UnicornArg::Ptr(engine), UnicornArg::Ptr(callback), UnicornArg::Ptr(0),
    ])?;
    eprintln!("STAGE=audio-callback-set");
    call(module, &emulator, "TTS_ENGINE_Initialize", vec![UnicornArg::Ptr(engine)])?;
    eprintln!("STAGE=initialized");
    let sample_rate = call(module, &emulator, "TTS_ENGINE_GetSamplingRate", vec![UnicornArg::Ptr(engine)])? as u32;
    call(module, &emulator, "TTS_ENGINE_VoiceControl", vec![
        UnicornArg::Ptr(engine), UnicornArg::I32(100), UnicornArg::I32(100), UnicornArg::I32(100),
    ])?;

    if let Some(stream) = stream {
        {
            let mut output = stream.lock().map_err(|_| anyhow::anyhow!("Samsung host output lock was poisoned"))?;
            let ready = format!("{{\"protocol\":1,\"sampleRate\":{},\"channels\":1,\"sampleWidth\":2,\"engineVersion\":{},\"cooperativeCancel\":true}}",
                sample_rate.max(24000), version);
            write_frame(&mut *output, b'R', ready.as_bytes())?;
        }

        let (sender, receiver) = mpsc::channel();
        let reader_cancel = cancel_requested.clone();
        thread::spawn(move || {
            let mut input = std::io::stdin().lock();
            loop {
                match read_command(&mut input) {
                    Ok(Some(command)) => {
                        if matches!(command, HostCommand::Stop) {
                            reader_cancel.store(true, Ordering::Release);
                        }
                        let should_quit = matches!(command, HostCommand::Quit);
                        if sender.send(command).is_err() || should_quit {
                            break;
                        }
                    }
                    Ok(None) => {
                        let _ = sender.send(HostCommand::Quit);
                        break;
                    }
                    Err(error) => {
                        let _ = sender.send(HostCommand::ReaderError(format!("{error:#}")));
                        break;
                    }
                }
            }
        });

        while let Ok(command) = receiver.recv() {
            match command {
                HostCommand::Speak(text) => {
                    cancelled.store(false, Ordering::Release);
                    produced_bytes.store(0, Ordering::Relaxed);
                    let result = call(module, &emulator, "TTS_ENGINE_InputText", vec![
                        UnicornArg::Ptr(engine), UnicornArg::Str(text),
                    ]);
                    let was_cancelled = cancelled.swap(false, Ordering::AcqRel)
                        || cancel_requested.swap(false, Ordering::AcqRel);
                    if was_cancelled {
                        call(module, &emulator, "TTS_ENGINE_Stop", vec![UnicornArg::Ptr(engine)])?;
                        let mut output = stream.lock().map_err(|_| anyhow::anyhow!("Samsung host output lock was poisoned"))?;
                        write_frame(&mut *output, b'C', &[])?;
                    } else {
                        let mut output = stream.lock().map_err(|_| anyhow::anyhow!("Samsung host output lock was poisoned"))?;
                        match result {
                            Ok(0) if produced_bytes.load(Ordering::Relaxed) > 0 => {
                                write_frame(&mut *output, b'D', &produced_bytes.load(Ordering::Relaxed).to_le_bytes())?;
                            }
                            Ok(code) => write_frame(&mut *output, b'E',
                                format!("Samsung synthesis returned {code}").as_bytes())?,
                            Err(error) => write_frame(&mut *output, b'E', format!("{error:#}").as_bytes())?,
                        }
                    }
                }
                HostCommand::Parameters(rate, pitch) => {
                    let result = call(module, &emulator, "TTS_ENGINE_VoiceControl", vec![
                        UnicornArg::Ptr(engine), UnicornArg::I32(pitch),
                        UnicornArg::I32(100), UnicornArg::I32(rate),
                    ]);
                    let mut output = stream.lock().map_err(|_| anyhow::anyhow!("Samsung host output lock was poisoned"))?;
                    match result {
                        Ok(0) => write_frame(&mut *output, b'K', &[])?,
                        Ok(code) => write_frame(&mut *output, b'E',
                            format!("Samsung parameter update returned {code}").as_bytes())?,
                        Err(error) => write_frame(&mut *output, b'E', format!("{error:#}").as_bytes())?,
                    }
                }
                HostCommand::Stop => {
                    cancel_requested.store(false, Ordering::Release);
                }
                HostCommand::Quit => break,
                HostCommand::ReaderError(error) => {
                    let mut output = stream.lock().map_err(|_| anyhow::anyhow!("Samsung host output lock was poisoned"))?;
                    write_frame(&mut *output, b'E', error.as_bytes())?;
                    break;
                }
            }
        }
        std::process::exit(0)
    }

    let text = text.context("one-shot text is missing")?;
    let output_path = output_path.context("one-shot output path is missing")?;
    let repeat_count = env::var("SAMSUNG_REPEAT")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(1)
        .max(1);
    let mut input_result = 0;
    let mut cancel_count = 0;
    for _ in 0..repeat_count {
        pcm.borrow_mut().clear();
        produced_bytes.store(0, Ordering::Relaxed);
        input_result = call(module, &emulator, "TTS_ENGINE_InputText", vec![
            UnicornArg::Ptr(engine), UnicornArg::Str(text.clone()),
        ])? as i32;
        if cancelled.swap(false, Ordering::AcqRel) {
            cancel_count += 1;
            emulator.clear_stop()?;
            call(module, &emulator, "TTS_ENGINE_Stop", vec![UnicornArg::Ptr(engine)])?;
            continue;
        }
        if input_result != 0 {
            break;
        }
    }

    let audio = pcm.borrow();
    println!("SMT_VERSION_NUMBER={version}");
    println!("SAMPLE_RATE={sample_rate}");
    println!("INPUT_RESULT={input_result}");
    println!("REPEAT_COUNT={repeat_count}");
    println!("CANCEL_COUNT={cancel_count}");
    println!("PCM_BYTES={}", audio.len());
    if input_result != 0 || audio.is_empty() { bail!("Samsung synthesis did not produce audio"); }
    write_wave(&output_path, sample_rate.max(24000), &audio)?;
    println!("OUTPUT={}", output_path.display());

    // RNIDBG currently corrupts the Windows heap while tearing down after
    // successful ARM64 execution. This one-shot proof exits after flushing.
    std::process::exit(0)
}
