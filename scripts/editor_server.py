#!/usr/bin/env python3
"""
editor_server.py — ショート動画カード型タイムラインエディタのローカルサーバー
使い方: python scripts/editor_server.py <project>
  work/<project>/timeline.json と materials.json を読み込んで配信する。
"""
import json
import mimetypes
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

PORT = 8766
SCRIPTS_DIR = Path(__file__).parent
REPO_ROOT = SCRIPTS_DIR.parent
EDITOR_DIR = REPO_ROOT / "editor"
WORK_ROOT = REPO_ROOT / "work"

# config.json で上書き可能（インストール後に各自設定）
_config_path = REPO_ROOT / "config.json"
_config = json.loads(_config_path.read_text(encoding="utf-8")) if _config_path.exists() else {}
SOUND_DIR = Path(_config.get("sound_dir", Path.home() / "shorts-editor" / "sounds"))

sys.path.insert(0, str(SCRIPTS_DIR))
from assign_clips import (  # noqa: E402
    _score, _signature, detect_keywords, pick_in_point, _pick_in_point_for_use, get_clips,
)
from render import render_timeline  # noqa: E402

project: str = ""
work_dir: Path = None
materials_dir: Path = None


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_VIDEO_EXTS = (".mp4", ".mov")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")   # ffmpegが直接扱える画像
_HEIC_EXTS = (".heic", ".heif")                     # sipsでjpgに変換して扱う


def _convert_heic_to_jpg(src: Path) -> Path:
    """HEIC/HEIFはffmpegが直接扱えないので、macOS標準のsipsで同フォルダに
    jpgを作って返す。以後はそのjpgを素材として扱う。"""
    dst = src.with_suffix(".jpg")
    if not dst.exists():
        subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dst)],
                       capture_output=True)
    return dst if dst.exists() else None


def _make_image_thumb(src: Path, base: str, frames_dir: Path) -> list:
    out = frames_dir / f"{base}_0.jpg"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(src),
                    "-vf", "scale=-2:640", "-frames:v", "1", "-q:v", "4", str(out)],
                   capture_output=True)
    if out.exists() and out.stat().st_size > 0:
        return [{"path": str(out), "t": 0}]
    return []


def _probe_duration(p: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _extract_frames(src: Path, base: str, dur: float, frames_dir: Path, n: int = 8) -> list:
    frames = []
    if dur <= 0:
        return frames
    for i in range(n):
        t = round(dur * (i + 0.5) / n, 2)
        out = frames_dir / f"{base}_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", str(t), "-i", str(src),
             "-frames:v", "1", "-vf", "scale=-2:640", "-q:v", "4", str(out)],
            capture_output=True)
        if out.exists() and out.stat().st_size > 0:
            frames.append({"path": str(out), "t": t})
    return frames


def scan_and_register_materials() -> dict:
    """materials_dir と materials.json を同期する。フォルダにある未登録の動画/画像を
    追加（duration取得＋サムネ抽出）し、フォルダから消えた素材の登録は削除する。
    タグ付けはしない（tag=either / memo=None）。手動の「動画素材フォルダを更新」ボタンから呼ぶ。"""
    mjson_path = work_dir / "materials.json"
    data = _load_json(mjson_path)
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    if not materials_dir.exists():
        data["_added"] = 0
        data["_removed"] = 0
        return data

    # フォルダから消えた素材の登録を削除する（幽霊素材の掃除）
    removed_files = [c["file"] for c in data["clips"]
                     if not (materials_dir / c["file"]).exists()]
    if removed_files:
        data["clips"] = [c for c in data["clips"]
                         if (materials_dir / c["file"]).exists()]
        # 消えた素材を使っているカードの割り当ても外す（未割当てに戻す）
        tl_path = work_dir / "timeline.json"
        if tl_path.exists():
            tl = _load_json(tl_path)
            gone = set(removed_files)
            changed = False
            for card in tl.get("cards", []):
                kept = [cl for cl in card.get("clips", []) if cl["file"] not in gone]
                if len(kept) != len(card.get("clips", [])):
                    card["clips"] = kept
                    changed = True
            if changed:
                _save_json(tl_path, tl)

    existing = {c["file"] for c in data["clips"]}

    all_exts = _VIDEO_EXTS + _IMAGE_EXTS + _HEIC_EXTS
    files = sorted([f for f in materials_dir.iterdir()
                    if f.is_file() and f.suffix.lower() in all_exts
                    and not f.name.startswith(".")])
    next_seq = max([c.get("sequence_no", 0) for c in data["clips"]], default=0) + 1
    added = 0
    for f in files:
        ext = f.suffix.lower()
        # HEICはjpgに変換して、以後jpgとして扱う
        if ext in _HEIC_EXTS:
            jpg = _convert_heic_to_jpg(f)
            if jpg is None:
                continue
            f, ext = jpg, ".jpg"
        if f.name in existing:
            continue
        if ext in _VIDEO_EXTS:
            dur = _probe_duration(f)
            frames = _extract_frames(f, f.name, dur, frames_dir)
            data["clips"].append({
                "file": f.name, "sequence_no": next_seq, "duration": dur,
                "frames": frames, "motion_ts": [],
                "tag": "either", "memo": None, "good_in": 0.5,
            })
        else:  # 画像（in点・尺の概念なし。カードの尺だけ表示する静止画）
            frames = _make_image_thumb(f, f.name, frames_dir)
            data["clips"].append({
                "file": f.name, "sequence_no": next_seq, "duration": 0,
                "frames": frames, "motion_ts": [],
                "tag": "either", "memo": None, "good_in": 0.0, "is_image": True,
            })
        existing.add(f.name)
        next_seq += 1
        added += 1

    if added or removed_files:
        _save_json(mjson_path, data)
    data["_added"] = added
    data["_removed"] = len(removed_files)
    return data


def _get_preview_proxy(filename: str) -> Path:
    """カメラ直撮り素材は10bit HEVC等、ブラウザが直接デコードできない形式の
    ことがある（実機テストで発覚：MEDIA_ELEMENT_ERROR）。エディタのプレビュー用に
    H.264の軽量プロキシを1回だけ生成してキャッシュする（高さ960に縮小、最終書き出し
    には影響しない。final.mp4は常にrender.pyが元素材から作る）"""
    proxies_dir = work_dir / "proxies"
    proxies_dir.mkdir(parents=True, exist_ok=True)
    proxy_path = proxies_dir / f"{filename}.proxy.mp4"

    if not proxy_path.exists():
        src = materials_dir / filename
        if src.suffix.lower() in _IMAGE_EXTS:
            # 静止画は3秒のループ動画プロキシにする（エディタの<video>で再生できるように）
            cmd = ["ffmpeg", "-y",
                   "-loop", "1", "-framerate", "30", "-t", "3", "-i", str(src),
                   "-f", "lavfi", "-t", "3", "-i", "anullsrc=r=48000:cl=stereo",
                   "-map", "0:v", "-map", "1:a",
                   "-vf", "scale=-2:960",
                   "-c:v", "h264_videotoolbox", "-q:v", "65",
                   "-c:a", "aac", "-b:a", "128k",
                   str(proxy_path)]
        else:
            cmd = ["ffmpeg", "-y", "-i", str(src),
                   "-vf", "scale=-2:960",
                   "-c:v", "h264_videotoolbox", "-q:v", "65",
                   "-c:a", "aac", "-b:a", "128k",
                   str(proxy_path)]
        subprocess.run(cmd, capture_output=True, check=True)

    return proxy_path


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    # ── GET ────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(EDITOR_DIR / "index.html", "text/html; charset=utf-8")

        elif path == "/api/timeline":
            tl = _load_json(work_dir / "timeline.json")
            vo = tl.get("voiceover_path")
            if vo:
                try:
                    import subprocess as _sp, json as _json
                    r = _sp.run(["ffprobe","-v","error","-show_entries","format=duration",
                                 "-of","json", vo], capture_output=True, text=True, check=True)
                    tl["voiceover_duration"] = float(_json.loads(r.stdout)["format"]["duration"])
                except Exception:
                    pass
            self._send_json(tl)

        elif path == "/api/materials":
            self._send_json(_load_json(work_dir / "materials.json"))

        elif path == "/api/clip":
            filename = query.get("file", [None])[0]
            if not filename:
                self.send_response(400)
                self.end_headers()
                return
            try:
                proxy_path = _get_preview_proxy(filename)
            except subprocess.CalledProcessError:
                self.send_response(500)
                self.end_headers()
                return
            self._serve_range_file(proxy_path)

        elif path == "/api/frame":
            clip_file = query.get("clip", [None])[0]
            n = int(query.get("n", ["0"])[0])
            materials = _load_json(work_dir / "materials.json")
            clip = next((c for c in materials["clips"] if c["file"] == clip_file), None)
            if not clip or not clip.get("frames"):
                self.send_response(404)
                self.end_headers()
                return
            frames = clip["frames"]
            frame = frames[min(n, len(frames) - 1)]
            # framesは[{"path","t"}]。pathは~/shorts-editorからの相対
            frame_path = (SCRIPTS_DIR.parent / frame["path"]).resolve()
            if not frame_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            self._send_file(frame_path, "image/jpeg")

        elif path == "/api/final":
            final_path = work_dir / "final.mp4"
            if not final_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            self._serve_range_file(final_path)

        elif path == "/api/sounds":
            sounds = []
            if SOUND_DIR.exists():
                for cat_dir in sorted(SOUND_DIR.iterdir()):
                    if cat_dir.is_dir() and not cat_dir.name.startswith("."):
                        for f in sorted(cat_dir.iterdir()):
                            if f.suffix.lower() == ".mp3":
                                sounds.append({
                                    "name": f.stem,
                                    "path": f"{cat_dir.name}/{f.name}",
                                    "category": cat_dir.name,
                                })
            self._send_json(sounds)

        elif path == "/api/sound":
            rel = query.get("file", [""])[0]
            sound_path = SOUND_DIR / rel
            if not sound_path.exists() or not sound_path.is_file():
                self.send_response(404); self.end_headers(); return
            self._serve_range_file(sound_path)

        elif path == "/api/export":
            final_path = work_dir / "final.mp4"
            if not final_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            size = final_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", 'attachment; filename="reel.mp4"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(final_path, "rb") as f:
                import shutil as _shutil
                _shutil.copyfileobj(f, self.wfile)

        else:
            self.send_response(404)
            self.end_headers()

    # ── POST ───────────────────────────────────────
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        data = json.loads(body) if body else {}

        if self.path == "/api/save":
            existing = _load_json(work_dir / "timeline.json")
            timeline = {"cards": data["cards"],
                        "voiceover_path": existing.get("voiceover_path")}
            _save_json(work_dir / "timeline.json", timeline)
            self._send_json({"ok": True})

        elif self.path == "/api/reassign":
            self._handle_reassign(data)

        elif self.path == "/api/render":
            self._handle_render(data)

        elif self.path == "/api/rescan":
            result = scan_and_register_materials()
            self._send_json(result)

        else:
            self.send_response(404)
            self.end_headers()

    # ── ハンドラ ───────────────────────────────────
    def _handle_reassign(self, data: dict):
        """指定カードについて、tag_filterに合うクリップを再選定する。
        素材は再利用可（他カードが使っていても候補にする）。同じ素材が他カードで
        既に使われている場合は、別のin点（別の瞬間）を割り当てて違うカットにする。"""
        card_id = data["card_id"]
        tag_filter = data.get("tag_filter", "either")

        timeline = _load_json(work_dir / "timeline.json")
        materials = _load_json(work_dir / "materials.json")
        cards = timeline["cards"]

        target = next((c for c in cards if c["id"] == card_id), None)
        if target is None:
            self._send_json({"ok": False, "error": "カードが見つかりません"})
            return

        candidates = materials["clips"]
        if not candidates:
            self._send_json({"ok": False, "error": "割当て可能な素材がありません"})
            return

        # 自動割当て（assign_clips._best_clip）と同じく、直前・直後のカードと
        # 同じ素材・同じ撮影（重複ファイル）は避ける（毎カット切り替わるように）。
        # 該当が無ければ制約を外して候補にする。
        target_idx = next((i for i, c in enumerate(cards) if c["id"] == card_id), None)
        avoid_files, avoid_sigs = set(), set()
        neighbor_indices = []
        if target_idx is not None:
            neighbor_indices = [target_idx - 1, target_idx + 1]
        for neighbor_idx in neighbor_indices:
            if not (0 <= neighbor_idx < len(cards)):
                continue
            for sub in get_clips(cards[neighbor_idx]):
                avoid_files.add(sub["file"])
                match = next((c for c in candidates if c["file"] == sub["file"]), None)
                if match:
                    avoid_sigs.add(_signature(match))

        keywords = [kw[0] for kw in detect_keywords(target["text"])]
        constrained = [c for c in candidates
                       if c["file"] not in avoid_files and _signature(c) not in avoid_sigs]
        pool = constrained or candidates
        best = max(pool, key=lambda c: _score(keywords, tag_filter, c))

        target_duration = target["end"] - target["start"]
        clip_duration = round(min(target_duration, best["duration"]), 3)
        # 同じ素材を使っている他カードのin点は避けて別の瞬間を選ぶ
        used_ins = set()
        for c in cards:
            if c["id"] == card_id:
                continue
            for sub in get_clips(c):
                if sub["file"] == best["file"]:
                    used_ins.add(round(sub["in"], 3))
        new_in = _pick_in_point_for_use(best, clip_duration, used_ins)
        new_clip = {"file": best["file"], "in": new_in, "duration": clip_duration}

        # タグ再選定はカードを単一クリップに戻す（素材ごと選び直す挙動）
        target["clips"] = [new_clip]
        target.pop("clip", None)
        target["tag_filter"] = tag_filter
        _save_json(work_dir / "timeline.json", timeline)

        self._send_json({"ok": True, "clips": target["clips"]})

    def _handle_render(self, data: dict):
        timeline = _load_json(work_dir / "timeline.json")
        voiceover_path = timeline.get("voiceover_path")
        jl_cut_offset = 0.2 if data.get("jl_cut") else 0.0

        try:
            render_timeline(timeline["cards"], materials_dir, work_dir / "final.mp4",
                             voiceover_path, jl_cut_offset)
            self._send_json({"ok": True, "output": str(work_dir / "final.mp4")})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)})

    def _send_file(self, path: Path, content_type: str):
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_range_file(self, path: Path):
        """Rangeリクエスト対応の汎用ファイル配信（複数の素材クリップ・final.mp4で共用）"""
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return

        size = path.stat().st_size
        mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
        rng = self.headers.get("Range")

        if rng:
            raw = rng.replace("bytes=", "")
            parts = raw.split("-")
            if parts[0] == "":
                suffix_len = int(parts[1])
                start = max(0, size - suffix_len)
                end = size - 1
            else:
                start = int(parts[0])
                end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
            length = end - start + 1

            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)


def main():
    global project, work_dir, materials_dir

    if len(sys.argv) < 2:
        print("使い方: python scripts/editor_server.py <project>")
        sys.exit(1)

    project = sys.argv[1]
    work_dir = WORK_ROOT / project

    timeline_path = work_dir / "timeline.json"
    materials_path = work_dir / "materials.json"
    if not timeline_path.exists() or not materials_path.exists():
        print(f"❌ {work_dir} に timeline.json / materials.json が見つかりません")
        print(f"   先に build_shorts.py を実行してください")
        sys.exit(1)

    materials_dir = Path(_load_json(materials_path)["materials_dir"])

    def _prewarm_proxies():
        clips = _load_json(materials_path).get("clips", [])
        for clip in clips:
            try:
                _get_preview_proxy(clip["file"])
            except Exception:
                pass
        print(f"   ✅ プロキシ事前生成完了 ({len(clips)}件)")

    threading.Thread(target=_prewarm_proxies, daemon=True).start()

    url = f"http://localhost:{PORT}"
    print(f"\n🌐 エディタ起動中... {url}")
    print(f"   プロジェクト: {project}")
    print(f"\n   終了するには Ctrl+C\n")

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 エディタを終了しました")


if __name__ == "__main__":
    main()
