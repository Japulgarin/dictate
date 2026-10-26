//! "Studio" export, in the spirit of Recordly (MIT): zooms toward where you clicked and
//! puts the recording on a gradient background with a margin and rounded corners.
//! It is one ffmpeg run driven by the cursor CSV saved during recording.

use crate::recorder::quality_args;
use anyhow::{bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

/// How far to zoom in on a click.
const ZOOM: f64 = 1.8;
/// Seconds to ease in and out of a zoom.
const RAMP_S: f64 = 0.45;
/// Start zooming this long before the click.
const LEAD_S: f64 = 0.4;
/// Stay zoomed this long after the last click of a group.
const HOLD_S: f64 = 1.8;
/// The recording fills this share of the frame; the rest is background.
const FRAME_SCALE: f64 = 0.88;
const CORNER_RADIUS: u32 = 18;
const BG_TOP: &str = "0x312e81";
const BG_BOTTOM: &str = "0x0f172a";

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sample {
    pub t: f64,
    pub x: f64,
    pub y: f64,
    pub down: bool,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Segment {
    pub start: f64,
    pub end: f64,
    pub x: f64,
    pub y: f64,
}

pub struct Track {
    /// Screen size the cursor coordinates refer to.
    pub screen: (f64, f64),
    pub samples: Vec<Sample>,
}

/// Reads the CSV written by `cursor::CursorTrack`. Unreadable lines are skipped.
pub fn parse_track(text: &str) -> Track {
    let mut screen = (1920.0, 1080.0);
    let mut samples = Vec::new();
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("# screen ") {
            let mut nums = rest.split_whitespace().filter_map(|v| v.parse::<f64>().ok());
            if let (Some(w), Some(h)) = (nums.next(), nums.next()) {
                screen = (w, h);
            }
            continue;
        }
        let f: Vec<&str> = line.split(',').collect();
        if f.len() != 4 {
            continue;
        }
        // The header row fails to parse and is skipped here.
        let (Ok(t), Ok(x), Ok(y), Ok(d)) =
            (f[0].parse::<f64>(), f[1].parse::<f64>(), f[2].parse::<f64>(), f[3].parse::<u8>())
        else {
            continue;
        };
        samples.push(Sample { t: t / 1000.0, x, y, down: d == 1 });
    }
    Track { screen, samples }
}

/// Clicks (button going down) grouped into zoom segments. A click that lands while
/// the previous segment is still zoomed joins it, and the segment is centred on the
/// average position of its clicks.
pub fn zoom_segments(samples: &[Sample]) -> Vec<Segment> {
    let clicks = samples
        .windows(2)
        .filter(|w| w[1].down && !w[0].down)
        .map(|w| w[1]);
    let mut segs: Vec<(Segment, f64)> = Vec::new(); // segment + number of clicks in it
    for c in clicks {
        match segs.last_mut() {
            Some((s, n)) if c.t - LEAD_S <= s.end => {
                s.x = (s.x * *n + c.x) / (*n + 1.0);
                s.y = (s.y * *n + c.y) / (*n + 1.0);
                s.end = c.t + HOLD_S;
                *n += 1.0;
            }
            _ => segs.push((
                Segment { start: (c.t - LEAD_S).max(0.0), end: c.t + HOLD_S, x: c.x, y: c.y },
                1.0,
            )),
        }
    }
    segs.into_iter().map(|(s, _)| s).collect()
}

/// ffmpeg `zoompan` expressions `(zoom, x, y)` for the segments. Cursor positions are
/// given as a fraction of `screen`, so they map onto any video size (`iw`, `ih`).
pub fn zoom_exprs(segs: &[Segment], screen: (f64, f64)) -> (String, String, String) {
    if segs.is_empty() {
        return ("1".into(), "0".into(), "0".into());
    }
    // Each segment eases 0 → 1 → 0 with a cosine curve; overlapping ones take the max.
    let amount = segs
        .iter()
        .map(|s| {
            format!(
                "(1-cos(PI*clip(min((it-{a:.3})/{r},({b:.3}-it)/{r}),0,1)))/2",
                a = s.start,
                b = s.end,
                r = RAMP_S
            )
        })
        .reduce(|acc, r| format!("max({acc},{r})"))
        .unwrap_or_default();
    let z = format!("1+{:.3}*{amount}", ZOOM - 1.0);

    let centre = |coord: fn(&Segment) -> f64, size: f64, dim: &str| {
        let mut e = format!("{dim}/2");
        for s in segs.iter().rev() {
            e = format!(
                "if(between(it,{:.3},{:.3}),{:.5}*{dim},{e})",
                s.start,
                s.end,
                coord(s) / size
            );
        }
        e
    };
    let cx = centre(|s| s.x, screen.0, "iw");
    let cy = centre(|s| s.y, screen.1, "ih");
    (
        z,
        format!("clip({cx}-iw/zoom/2,0,iw-iw/zoom)"),
        format!("clip({cy}-ih/zoom/2,0,ih-ih/zoom)"),
    )
}

/// Renders `<stamp>.studio.mp4` next to `video`, using `<stamp>.cursor.csv` if present.
pub fn render(ffmpeg: &str, encoder: &str, fps: u32, video: &Path) -> Result<PathBuf> {
    let (w, h, duration) = probe(ffmpeg, video)?;
    let track = fs::read_to_string(video.with_extension("cursor.csv"))
        .ok()
        .map(|t| parse_track(&t));
    let segs = track.as_ref().map(|t| zoom_segments(&t.samples)).unwrap_or_default();
    let screen = track.as_ref().map(|t| t.screen).unwrap_or((w as f64, h as f64));
    let (z, x, y) = zoom_exprs(&segs, screen);

    let fw = even(w as f64 * FRAME_SCALE);
    let fh = even(h as f64 * FRAME_SCALE);
    let mask = video.with_extension("mask.png");
    make_mask(ffmpeg, fw, fh, CORNER_RADIUS, &mask)?;

    let dur = format!("{duration:.3}");
    let graph = format!(
        "[0:v]zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps},scale={fw}:{fh},format=yuva420p[fg0];\
         [1:v]format=gray,scale={fw}:{fh}[m];\
         [fg0][m]alphamerge[fg];\
         gradients=s={w}x{h}:c0={BG_TOP}:c1={BG_BOTTOM}:x0=0:y0=0:x1={w}:y1={h}:r={fps}:d={dur}[bg];\
         [bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p[v]"
    );
    let out = video.with_extension("studio.mp4");
    let status = Command::new(ffmpeg)
        .args(["-hide_banner", "-loglevel", "error", "-y", "-i"])
        .arg(video)
        .args(["-loop", "1", "-t", &dur, "-i"])
        .arg(&mask)
        .args(["-filter_complex", &graph, "-map", "[v]", "-map", "0:a?", "-c:v", encoder])
        .args(quality_args(encoder))
        .args(["-c:a", "copy", "-shortest"])
        .arg(&out)
        .status()
        .context("can't start ffmpeg for the studio export")?;
    let _ = fs::remove_file(&mask);
    if !status.success() {
        bail!("ffmpeg studio export exited with {status}");
    }
    Ok(out)
}

fn even(v: f64) -> u32 {
    (v as u32) & !1
}

/// A white rounded rectangle on black, used as the alpha mask for the corners.
fn make_mask(ffmpeg: &str, w: u32, h: u32, r: u32, out: &Path) -> Result<()> {
    let (hw, hh) = (w as f64 / 2.0, h as f64 / 2.0);
    let geq = format!(
        "geq=lum='255*lte(hypot(max(0,abs(X-{hw})-{ix}),max(0,abs(Y-{hh})-{iy})),{r})'",
        ix = hw - r as f64,
        iy = hh - r as f64,
    );
    let status = Command::new(ffmpeg)
        .args(["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i"])
        .arg(format!("color=c=white:s={w}x{h}:d=1"))
        .args(["-vf", &format!("format=gray,{geq}"), "-frames:v", "1"])
        .arg(out)
        .status()
        .context("can't start ffmpeg to draw the corner mask")?;
    if !status.success() {
        bail!("ffmpeg corner mask exited with {status}");
    }
    Ok(())
}

/// Video width, height and duration in seconds.
fn probe(ffmpeg: &str, video: &Path) -> Result<(u32, u32, f64)> {
    let out = Command::new(ffprobe_path(ffmpeg))
        .args(["-v", "error", "-select_streams", "v:0"])
        .args(["-show_entries", "stream=width,height:format=duration", "-of", "default=nw=1"])
        .arg(video)
        .output()
        .context("can't run ffprobe")?;
    let text = String::from_utf8_lossy(&out.stdout);
    let get = |key: &str| {
        text.lines()
            .find_map(|l| l.strip_prefix(key)?.strip_prefix('=').map(str::trim).map(String::from))
    };
    let w = get("width").and_then(|v| v.parse().ok());
    let h = get("height").and_then(|v| v.parse().ok());
    let d = get("duration").and_then(|v| v.parse().ok());
    match (w, h, d) {
        (Some(w), Some(h), Some(d)) => Ok((w, h, d)),
        _ => bail!("ffprobe couldn't read {}", video.display()),
    }
}

/// `ffprobe` next to the configured `ffmpeg`, or plain `ffprobe` from PATH.
fn ffprobe_path(ffmpeg: &str) -> PathBuf {
    let p = Path::new(ffmpeg);
    match p.file_name().and_then(|n| n.to_str()) {
        Some(n) if n.to_ascii_lowercase().starts_with("ffmpeg") => {
            let name = if n.to_ascii_lowercase().ends_with(".exe") { "ffprobe.exe" } else { "ffprobe" };
            p.with_file_name(name)
        }
        _ => PathBuf::from("ffprobe"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CSV: &str = "# screen 1920 1080\nt_ms,x,y,left\n0,10,10,0\n1000,960,540,1\n1100,960,540,0\n1500,1000,600,1\n1600,1000,600,0\n9000,100,100,1\n";

    #[test]
    fn parses_header_screen_and_rows() {
        let t = parse_track(CSV);
        assert_eq!(t.screen, (1920.0, 1080.0));
        assert_eq!(t.samples.len(), 6);
        assert_eq!(t.samples[1], Sample { t: 1.0, x: 960.0, y: 540.0, down: true });
    }

    #[test]
    fn close_clicks_merge_and_far_clicks_split() {
        let segs = zoom_segments(&parse_track(CSV).samples);
        assert_eq!(segs.len(), 2);
        // First two clicks (1.0 s and 1.5 s) form one segment centred between them.
        assert!((segs[0].start - 0.6).abs() < 1e-9);
        assert!((segs[0].end - 3.3).abs() < 1e-9);
        assert_eq!((segs[0].x, segs[0].y), (980.0, 570.0));
        assert!((segs[1].start - 8.6).abs() < 1e-9);
    }

    #[test]
    fn button_held_from_the_start_is_not_a_click() {
        let s = parse_track("0,1,1,1\n16,1,1,1\n");
        assert!(zoom_segments(&s.samples).is_empty());
    }

    #[test]
    fn no_clicks_means_no_zoom() {
        assert_eq!(zoom_exprs(&[], (1920.0, 1080.0)).0, "1");
    }

    #[test]
    fn exprs_use_fractions_of_the_screen() {
        let seg = Segment { start: 1.0, end: 3.0, x: 960.0, y: 270.0 };
        let (z, x, y) = zoom_exprs(&[seg], (1920.0, 1080.0));
        assert!(z.starts_with("1+0.800*"));
        assert!(x.contains("0.50000*iw"));
        assert!(y.contains("0.25000*ih"));
    }

    #[test]
    fn ffprobe_sits_next_to_ffmpeg() {
        assert_eq!(ffprobe_path("C:/x/ffmpeg.exe"), PathBuf::from("C:/x/ffprobe.exe"));
        assert_eq!(ffprobe_path("ffmpeg"), PathBuf::from("ffprobe"));
    }
}
