//! Screen + microphone recording.
//!
//! Video: ffmpeg's `gdigrab` records the desktop into a temporary mp4.
//! Audio: `cpal` records the default microphone into a temporary raw PCM file.
//! On stop, ffmpeg muxes both into the final mp4 and the temporary files are removed.

use crate::config::Config;
use crate::cursor::CursorTrack;
use anyhow::{anyhow, bail, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

const AUDIO_CHANNELS: u16 = 1;

/// An open microphone capture writing raw PCM to `raw`.
struct Mic {
    /// Keeps the stream alive; dropping it stops capture.
    stream: cpal::Stream,
    writer: Arc<Mutex<BufWriter<File>>>,
    rate: u32,
    raw: PathBuf,
}

pub struct Recorder {
    video: Child,
    video_tmp: PathBuf,
    mic: Option<Mic>,
    cursor: Option<CursorTrack>,
    out: PathBuf,
    ffmpeg: String,
    /// One ffmpeg process wrote the final file (screen + mic), so there is nothing to mux.
    single: bool,
}

impl Recorder {
    pub fn start(cfg: &Config) -> Result<Self> {
        let out_dir = resolve_dir(&cfg.recordings_dir);
        fs::create_dir_all(&out_dir)
            .with_context(|| format!("can't create {}", out_dir.display()))?;
        let stamp = timestamp();
        let out = out_dir.join(format!("{stamp}.mp4"));
        let video_tmp = out_dir.join(format!("{stamp}.video.mp4"));
        let audio_tmp = out_dir.join(format!("{stamp}.audio.raw"));

        let single = cfg.record_mic && !cfg.mic_device.is_empty();
        let (video, video_tmp) = if single {
            (spawn_av(&cfg.ffmpeg, &cfg.record_encoder, cfg.record_fps, &cfg.mic_device, &cfg.record_region, &out)?, out.clone())
        } else {
            (spawn_video(&cfg.ffmpeg, &cfg.record_encoder, cfg.record_fps, &cfg.record_region, &video_tmp)?, video_tmp)
        };

        let cursor = if cfg.record_cursor {
            match CursorTrack::start(&out_dir.join(format!("{stamp}.cursor.csv"))) {
                Ok(c) => Some(c),
                Err(e) => {
                    eprintln!("cursor tracking unavailable: {e:#}");
                    None
                }
            }
        } else {
            None
        };

        let mic = if cfg.record_mic && !single {
            match start_mic(&audio_tmp) {
                Ok(m) => Some(m),
                Err(e) => {
                    // Keep recording the screen even if the mic is missing.
                    eprintln!("microphone unavailable, recording without audio: {e:#}");
                    None
                }
            }
        } else {
            None
        };

        Ok(Self {
            video,
            video_tmp,
            mic: mic.map(|(stream, writer, rate)| Mic {
                stream,
                writer,
                rate,
                raw: audio_tmp,
            }),
            cursor,
            out,
            ffmpeg: cfg.ffmpeg.clone(),
            single,
        })
    }

    /// Stop both captures, mux them into the final file, and return its path.
    pub fn stop(mut self) -> Result<PathBuf> {
        if let Some(c) = self.cursor.take() {
            c.stop();
        }
        let audio = self.stop_mic()?;

        // Ask ffmpeg to finish the file cleanly ("q" on stdin), then wait for it.
        if let Some(mut stdin) = self.video.stdin.take() {
            let _ = stdin.write_all(b"q\n");
        }
        let _ = self.video.wait();

        if !self.video_tmp.exists() || fs::metadata(&self.video_tmp)?.len() == 0 {
            bail!("ffmpeg produced no video (check that gdigrab works)");
        }

        if self.single {
            return Ok(self.out.clone());
        }
        mux(&self.ffmpeg, &self.video_tmp, audio.as_ref(), &self.out)?;

        let _ = fs::remove_file(&self.video_tmp);
        if let Some((_, raw)) = audio {
            let _ = fs::remove_file(raw);
        }
        Ok(self.out.clone())
    }

    /// Stops the mic and flushes its file. Returns (sample rate, raw PCM path) if audio was captured.
    fn stop_mic(&mut self) -> Result<Option<(u32, PathBuf)>> {
        let Some(mic) = self.mic.take() else {
            return Ok(None);
        };
        drop(mic.stream); // stops the callback before we flush
        mic.writer
            .lock()
            .map_err(|_| anyhow!("mic writer poisoned"))?
            .flush()?;
        Ok(Some((mic.rate, mic.raw)))
    }
}

impl Drop for Recorder {
    fn drop(&mut self) {
        // If the recorder is dropped without stop(), don't leave ffmpeg running.
        let _ = self.video.kill();
        let _ = self.video.wait();
    }
}

fn resolve_dir(dir: &str) -> PathBuf {
    let p = PathBuf::from(dir);
    if p.is_absolute() {
        p
    } else {
        crate::config::app_dir().join(p)
    }
}

/// Speed/quality flags for the configured H.264 encoder.
pub fn quality_args(encoder: &str) -> &'static [&'static str] {
    if encoder == "libx264" {
        &["-preset", "veryfast", "-crf", "23"]
    } else {
        &["-preset", "p4", "-cq", "23"]
    }
}

/// The part of the desktop to record as gdigrab input options: the full screen, or its left or right half.
fn capture_args(region: &str) -> Vec<String> {
    use windows::Win32::UI::WindowsAndMessaging::SetProcessDPIAware;
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};
    // gdigrab works in real pixels: without this, a 125 % display reports its smaller logical size.
    let (w, h) = unsafe {
        let _ = SetProcessDPIAware();
        (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN))
    };
    let half = (w / 2) & !1; // even sizes, needed by h264
    let h = h & !1;
    let x = match region {
        "left" => 0,
        "right" => w - half,
        _ => return Vec::new(),
    };
    vec!["-offset_x".into(), x.to_string(), "-video_size".into(), format!("{half}x{h}")]
}

/// Screen (gdigrab) and microphone (dshow) in one ffmpeg process, written straight to `out`.
fn spawn_av(ffmpeg: &str, encoder: &str, fps: u32, mic: &str, region: &str, out: &Path) -> Result<Child> {
    let child = Command::new(ffmpeg)
        .args(["-hide_banner", "-loglevel", "error", "-y"])
        .args(["-f", "gdigrab", "-framerate", &fps.to_string()])
        .args(capture_args(region))
        .args(["-i", "desktop"])
        .args(["-f", "dshow", "-i", &format!("audio={mic}")])
        .args(["-c:v", encoder, "-pix_fmt", "yuv420p"])
        .args(quality_args(encoder))
        .args(["-c:a", "aac", "-b:a", "160k"])
        .arg(out)
        .stdin(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .with_context(|| format!("can't start ffmpeg ({ffmpeg}) for screen + mic"))?;
    Ok(child)
}

fn spawn_video(ffmpeg: &str, encoder: &str, fps: u32, region: &str, out: &Path) -> Result<Child> {
    let child = Command::new(ffmpeg)
        .args(["-hide_banner", "-loglevel", "error", "-y"])
        .args(["-f", "gdigrab", "-framerate", &fps.to_string()])
        .args(capture_args(region))
        .args(["-i", "desktop"])
        .args(["-c:v", encoder, "-pix_fmt", "yuv420p"])
        .args(quality_args(encoder))
        .arg(out)
        .stdin(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .with_context(|| format!("can't start ffmpeg ({ffmpeg}); set `ffmpeg` in config.toml"))?;
    Ok(child)
}

/// Opens the default microphone and writes mono 16-bit PCM to `raw`.
fn start_mic(raw: &Path) -> Result<(cpal::Stream, Arc<Mutex<BufWriter<File>>>, u32)> {
    let host = cpal::default_host();
    let device = host.default_input_device().context("no microphone found")?;
    let supported = device.default_input_config()?;
    let rate = supported.sample_rate().0;
    let channels = supported.channels() as usize;
    let config: cpal::StreamConfig = supported.clone().into();
    let writer = Arc::new(Mutex::new(BufWriter::new(File::create(raw)?)));
    let err = |e| eprintln!("mic stream error: {e}");

    let stream = match supported.sample_format() {
        cpal::SampleFormat::F32 => {
            let w = Arc::clone(&writer);
            device.build_input_stream(
                &config,
                move |data: &[f32], _| {
                    let mut out = w.lock().unwrap();
                    for frame in data.chunks(channels) {
                        let mono = frame.iter().sum::<f32>() / channels as f32;
                        let s = (mono.clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
                        let _ = out.write_all(&s.to_le_bytes());
                    }
                },
                err,
                None,
            )?
        }
        cpal::SampleFormat::I16 => {
            let w = Arc::clone(&writer);
            device.build_input_stream(
                &config,
                move |data: &[i16], _| {
                    let mut out = w.lock().unwrap();
                    for frame in data.chunks(channels) {
                        let sum: i32 = frame.iter().map(|&s| s as i32).sum();
                        let s = (sum / channels as i32) as i16;
                        let _ = out.write_all(&s.to_le_bytes());
                    }
                },
                err,
                None,
            )?
        }
        other => bail!("unsupported microphone sample format: {other:?}"),
    };
    stream.play()?;
    Ok((stream, writer, rate))
}

/// Combine the video (and optional raw audio) into the final file.
fn mux(ffmpeg: &str, video: &Path, audio: Option<&(u32, PathBuf)>, out: &Path) -> Result<()> {
    let mut cmd = Command::new(ffmpeg);
    cmd.args(["-hide_banner", "-loglevel", "error", "-y", "-i"]).arg(video);
    match audio {
        Some((rate, raw)) => {
            cmd.args(["-f", "s16le", "-ar", &rate.to_string(), "-ac", &AUDIO_CHANNELS.to_string(), "-i"])
                .arg(raw)
                .args(["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest"]);
        }
        None => {
            cmd.args(["-map", "0:v:0", "-c:v", "copy", "-an"]);
        }
    }
    let status = cmd.arg(out).status().context("ffmpeg mux failed to start")?;
    if !status.success() {
        bail!("ffmpeg mux exited with {status}");
    }
    Ok(())
}

/// Local time as `YYYY-MM-DD_HHMMSS`, used for file names.
fn timestamp() -> String {
    #[cfg(windows)]
    {
        use windows::Win32::System::SystemInformation::GetLocalTime;
        let t = unsafe { GetLocalTime() };
        format!(
            "{:04}-{:02}-{:02}_{:02}{:02}{:02}",
            t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond
        )
    }
    #[cfg(not(windows))]
    {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        format!("{secs}")
    }
}
