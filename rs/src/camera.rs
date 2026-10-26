//! Webcam preview: a round picture shown under the bubble.
//!
//! Frames come from ffmpeg's `dshow` input, written as raw RGB on stdout. A reader
//! thread keeps only the newest frame; `paint_preview` turns it into a texture and
//! paints it inside a circle. The circle is drawn by `paint_round`, which the later
//! editor and export can reuse so the preview and the final video look the same.

use crate::config::Config;
use anyhow::{anyhow, Context, Result};
use eframe::egui::{
    self,
    epaint::{Mesh, Vertex},
    Color32, Pos2, Rect, Shape, Stroke, TextureHandle, TextureOptions, Vec2,
};
use std::io::Read;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;

/// Side length of the round preview, in points (the frames are this many pixels).
pub const PREVIEW_SIDE: f32 = 240.0;
const SIDE: usize = PREVIEW_SIDE as usize;
const FRAME_BYTES: usize = SIDE * SIDE * 3;

/// State shared between the reader thread and the UI.
#[derive(Default)]
pub struct Shared {
    latest: Mutex<Option<Vec<u8>>>,
    texture: Mutex<Option<TextureHandle>>,
}

pub struct CameraFeed {
    child: Child,
    shared: Arc<Shared>,
}

impl CameraFeed {
    pub fn start(cfg: &Config) -> Result<Self> {
        let device = pick_camera(&cfg.ffmpeg, &cfg.camera_device)?;
        let mut child = Command::new(&cfg.ffmpeg)
            .args(["-hide_banner", "-loglevel", "error"])
            .args(["-f", "dshow", "-framerate", "30", "-video_size", "320x240"])
            .arg("-i")
            .arg(format!("video={device}"))
            // Mirror, then take the centred square.
            .args(["-vf", "hflip,crop=240:240:40:0"])
            .args(["-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .with_context(|| format!("can't start ffmpeg for camera {device:?}"))?;

        let mut stdout = child.stdout.take().context("no camera output pipe")?;
        let shared = Arc::new(Shared::default());
        let writer = Arc::clone(&shared);
        // The thread ends by itself when the pipe closes (ffmpeg killed or camera gone).
        thread::spawn(move || {
            let mut buf = vec![0u8; FRAME_BYTES];
            while stdout.read_exact(&mut buf).is_ok() {
                if let Ok(mut latest) = writer.latest.lock() {
                    *latest = Some(buf.clone());
                }
            }
        });

        Ok(Self { child, shared })
    }

    pub fn shared(&self) -> Arc<Shared> {
        Arc::clone(&self.shared)
    }
}

impl Drop for CameraFeed {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// The camera named in config, or the first video device ffmpeg lists.
fn pick_camera(ffmpeg: &str, wanted: &str) -> Result<String> {
    if !wanted.is_empty() {
        return Ok(wanted.to_string());
    }
    // `-list_devices` always exits with an error, so only stderr matters.
    let out = Command::new(ffmpeg)
        .args(["-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        .output()
        .context("can't run ffmpeg to list cameras")?;
    let text = String::from_utf8_lossy(&out.stderr);
    text.lines()
        .find(|l| l.contains("(video)"))
        .and_then(|l| {
            let start = l.find('"')? + 1;
            let end = l.rfind('"')?;
            (end > start).then(|| l[start..end].to_string())
        })
        .ok_or_else(|| anyhow!("no camera found (set `camera_device` in config.toml)"))
}

/// Paints the newest camera frame as a circle inside `rect`, which should be a
/// `PREVIEW_SIDE` square. Call once per frame while the camera is on.
pub fn paint_preview(ui: &mut egui::Ui, shared: &Shared, rect: Rect) {
    let ctx = ui.ctx().clone();

    // Upload the newest frame, reusing the texture when possible.
    let frame = shared.latest.lock().ok().and_then(|mut l| l.take());
    if let Some(px) = frame {
        let image = egui::ColorImage::from_rgb([SIDE, SIDE], &px);
        if let Ok(mut tex) = shared.texture.lock() {
            match tex.as_mut() {
                Some(t) => t.set(image, TextureOptions::LINEAR),
                None => *tex = Some(ctx.load_texture("camera", image, TextureOptions::LINEAR)),
            }
        }
    }

    let tex = shared.texture.lock().ok().and_then(|t| t.as_ref().map(|t| t.id()));
    match tex {
        Some(id) => paint_round(ui.painter(), rect, id),
        None => {
            ui.painter().circle_filled(rect.center(), rect.width() / 2.0, Color32::from_gray(30));
        }
    }

    // Keep frames flowing while the preview is on screen.
    ctx.request_repaint();
}

/// Draws the texture inside a circle: a triangle fan whose UVs follow the circle.
/// Shared with the editor so the overlay looks the same in preview and export.
pub fn paint_round(painter: &egui::Painter, rect: Rect, tex: egui::TextureId) {
    const N: u32 = 64;
    let c = rect.center();
    let r = rect.width() / 2.0;
    let mut mesh = Mesh::with_texture(tex);
    mesh.vertices.push(Vertex { pos: c, uv: Pos2::new(0.5, 0.5), color: Color32::WHITE });
    for i in 0..=N {
        let a = i as f32 / N as f32 * std::f32::consts::TAU;
        let (s, co) = a.sin_cos();
        mesh.vertices.push(Vertex {
            pos: c + Vec2::new(co, s) * r,
            uv: Pos2::new(0.5 + 0.5 * co, 0.5 + 0.5 * s),
            color: Color32::WHITE,
        });
    }
    for i in 1..=N {
        mesh.add_triangle(0, i, i + 1);
    }
    painter.add(Shape::mesh(mesh));
    painter.circle_stroke(c, r - 1.0, Stroke::new(2.0_f32, Color32::from_white_alpha(70)));
}
