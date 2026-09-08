"""
YouTube動画からサムネイル候補を自動生成するスクリプト

処理の流れ：
1. Google Driveの入力フォルダから指定された動画ファイルをダウンロード
2. ffmpegで動画から一定間隔でフレーム（静止画）を切り出す
3. Gemini APIで各フレームを「サムネイルとしての良さ」で採点
4. 上位フレームに、指定したタイトル文字を2パターンで合成
5. 完成した候補画像をGoogle Driveの出力フォルダにアップロード

必要な環境変数（GitHub Secretsから渡す想定）：
- GOOGLE_SERVICE_ACCOUNT_JSON : サービスアカウントの認証情報（JSON文字列）
- GEMINI_API_KEY              : Gemini APIキー
- DRIVE_INPUT_FOLDER_ID       : 動画ファイルを置く入力フォルダのID
- DRIVE_OUTPUT_FOLDER_ID      : サムネイル候補を保存する出力フォルダのID

ワークフロー実行時の入力（GitHub Actionsのinputsから渡す想定）：
- VIDEO_FILE_NAME : 入力フォルダ内の対象動画ファイル名
- TITLE_TEXT      : サムネイルに載せるメインタイトル
- SUBTITLE_TEXT   : サムネイルに載せるサブタイトル（任意）
"""

import os
import io
import json
import subprocess
import time
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google import genai
from PIL import Image, ImageDraw, ImageFont

WORK_DIR = Path("work")
FRAMES_DIR = WORK_DIR / "frames"
CANDIDATES_DIR = WORK_DIR / "candidates"

FRAME_INTERVAL_SEC = 15  # 何秒おきにフレームを切り出すか（無料枠のクォータに収まるよう広めに設定）
MAX_FRAMES = 15          # 切り出すフレームの上限（Gemini無料枠は1日20回程度のため余裕を持たせる）
TOP_N = 4                # 採点上位いくつを候補にするか

# ワークフロー側で `apt-get install fonts-noto-cjk` してある前提のパス
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def get_drive_service():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def find_file_id(drive, folder_id, file_name):
    query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
    results = drive.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if not files:
        raise FileNotFoundError(f"入力フォルダに '{file_name}' が見つかりません")
    return files[0]["id"]


def download_file(drive, file_id, dest_path: Path):
    request = drive.files().get_media(fileId=file_id)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_file(drive, local_path: Path, folder_id, name):
    file_metadata = {"name": name, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=True)
    drive.files().create(body=file_metadata, media_body=media, fields="id").execute()


def extract_frames(video_path: Path):
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    fps = f"1/{FRAME_INTERVAL_SEC}"
    subprocess.run(
        [
            "ffmpeg", "-i", str(video_path),
            "-vf", f"fps={fps}",
            "-frames:v", str(MAX_FRAMES),
            str(FRAMES_DIR / "frame_%04d.jpg"),
            "-y",
        ],
        check=True,
    )
    return sorted(FRAMES_DIR.glob("frame_*.jpg"))


def score_frame(client, frame_path: Path, max_retries=3):
    image = Image.open(frame_path)
    prompt = (
        "この画像はYouTube動画のワンシーンです。YouTubeのサムネイル画像として"
        "使うのにどれくらい適しているか、次の観点で0〜10点で採点してください。\n"
        "・ブレやぼやけがない\n"
        "・人物が写っている場合は表情や向きが良い\n"
        "・構図として見やすい\n"
        "・単調すぎない（何かしら動きや意味のある瞬間）\n"
        'JSON形式のみで {"score": 数値, "reason": "短い理由"} を返してください。'
        "他の文章は含めないでください。"
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[prompt, image],
            )
            text = response.text.strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                data = json.loads(text)
                return float(data.get("score", 0)), data.get("reason", "")
            except (json.JSONDecodeError, ValueError):
                return 0.0, "採点結果の解析に失敗"
        except Exception as e:  # noqa: BLE001 - APIエラー全般をリトライ対象にする
            last_error = e
            wait_sec = 20 * (attempt + 1)
            print(
                f"  Gemini API呼び出しに失敗（{attempt + 1}/{max_retries}回目）: {e}\n"
                f"  {wait_sec}秒待って再試行します"
            )
            time.sleep(wait_sec)

    print(f"  {frame_path.name}: リトライ上限に達したためスキップします（{last_error}）")
    return 0.0, "APIエラーのため未採点"


def compose_thumbnail(frame_path: Path, title_text, subtitle_text, pattern, out_path: Path):
    img = Image.open(frame_path).convert("RGB")
    img = img.resize((1280, 720))
    draw = ImageDraw.Draw(img, "RGBA")
    title_font = ImageFont.truetype(FONT_PATH, 84)
    subtitle_font = ImageFont.truetype(FONT_PATH, 40)

    if pattern == "A":
        # 情報訴求型：下部に暗い帯を敷いてタイトルを載せる
        draw.rectangle([(0, 500), (1280, 720)], fill=(0, 0, 0, 140))
        draw.text((48, 540), title_text, font=title_font, fill=(255, 255, 255, 255))
        if subtitle_text:
            draw.text((48, 640), subtitle_text, font=subtitle_font, fill=(240, 153, 123, 255))
    else:
        # 情緒訴求型：中央に大きくタイトルを置く
        draw.rectangle([(0, 0), (1280, 720)], fill=(216, 90, 48, 30))
        bbox = draw.textbbox((0, 0), title_text, font=title_font)
        w = bbox[2] - bbox[0]
        draw.text(((1280 - w) / 2, 300), title_text, font=title_font, fill=(255, 255, 255, 255))
        if subtitle_text:
            bbox_s = draw.textbbox((0, 0), subtitle_text, font=subtitle_font)
            w_s = bbox_s[2] - bbox_s[0]
            draw.text(
                ((1280 - w_s) / 2, 420), subtitle_text,
                font=subtitle_font, fill=(245, 196, 179, 255),
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, quality=92)


def main():
    video_file_name = os.environ["VIDEO_FILE_NAME"]
    title_text = os.environ["TITLE_TEXT"]
    subtitle_text = os.environ.get("SUBTITLE_TEXT", "")
    input_folder_id = os.environ["DRIVE_INPUT_FOLDER_ID"]
    output_folder_id = os.environ["DRIVE_OUTPUT_FOLDER_ID"]

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    drive = get_drive_service()

    print(f"[1/5] 動画を検索・ダウンロード中: {video_file_name}")
    file_id = find_file_id(drive, input_folder_id, video_file_name)
    video_path = WORK_DIR / video_file_name
    download_file(drive, file_id, video_path)

    print("[2/5] フレームを切り出し中")
    frames = extract_frames(video_path)
    print(f"  {len(frames)} 枚のフレームを抽出しました")

    print("[3/5] Gemini APIで採点中")
    scored = []
    for frame in frames:
        score, reason = score_frame(client, frame)
        scored.append((score, frame, reason))
        print(f"  {frame.name}: {score}点 ({reason})")

    scored.sort(key=lambda x: x[0], reverse=True)
    top_frames = scored[:TOP_N]

    print("[4/5] サムネイル候補を合成中")
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    output_paths = []
    for rank, (score, frame, reason) in enumerate(top_frames, start=1):
        for pattern in ("A", "B"):
            out_name = f"candidate_{rank}_pattern{pattern}_{video_file_name}.jpg"
            out_path = CANDIDATES_DIR / out_name
            compose_thumbnail(frame, title_text, subtitle_text, pattern, out_path)
            output_paths.append(out_path)

    print("[5/5] Google Driveにアップロード中")
    for path in output_paths:
        upload_file(drive, path, output_folder_id, path.name)
        print(f"  アップロード完了: {path.name}")

    print("完了しました。出力フォルダを確認してください。")


if __name__ == "__main__":
    main()
