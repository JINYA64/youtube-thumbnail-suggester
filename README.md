# youtube-thumbnail-suggester

今治市公式YouTubeチャンネルの動画から、AIでサムネイル候補を自動生成する仕組みです。

## できること

1. Google Driveに置いた動画ファイルから一定間隔でフレームを切り出す
2. Gemini APIで各フレームを「サムネイルとしての良さ」で自動採点
3. 上位フレームに、指定したタイトル文字を2パターン（情報訴求型／情緒訴求型）で合成
4. 完成した候補画像をGoogle Driveの出力フォルダに保存

**候補を作るところまでが自動化の範囲です。実際にYouTubeへ設定するのは、
候補を見て人が選ぶ運用にしてください**（市民や職員の顔が写るフレームもあるため）。

## 事前準備

### 1. Google Driveのフォルダを2つ用意する

- 入力フォルダ：元になる動画ファイルを置く場所
- 出力フォルダ：できたサムネイル候補が保存される場所

それぞれのフォルダIDは、Driveでフォルダを開いたときのURLの末尾の文字列です。
`https://drive.google.com/drive/folders/【ここがフォルダID】`

### 2. サービスアカウントを用意する

photo-curatorで作成したサービスアカウントを流用する場合は、上記2つの
フォルダをそのサービスアカウントのメールアドレス
（〜@〜.iam.gserviceaccount.com）と共有するだけでOKです。

新しく作る場合は、Google Cloud Consoleで新規プロジェクト→サービスアカウント
作成→JSONキーをダウンロード→そのフォルダと共有、という流れになります。

### 3. Gemini APIキーを用意する

Google AI StudioでAPIキーを発行します（他プロジェクトと分けて新規発行を推奨）。

### 4. このリポジトリにGitHub Secretsを設定する

リポジトリの Settings → Secrets and variables → Actions で、以下を登録してください。

| Secret名 | 内容 |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | サービスアカウントのJSONキーの中身をそのまま貼り付け |
| `GEMINI_API_KEY` | Gemini APIキー |
| `DRIVE_INPUT_FOLDER_ID` | 入力フォルダのID |
| `DRIVE_OUTPUT_FOLDER_ID` | 出力フォルダのID |

## 使い方

1. 対象の動画ファイルを、入力フォルダにアップロードしておく
2. GitHubのActionsタブ →「サムネイル候補生成」→「Run workflow」
3. 入力欄に以下を入力して実行
   - `video_file_name`：入力フォルダ内の動画ファイル名（拡張子まで）
   - `title_text`：サムネイルに載せたいメインタイトル
   - `subtitle_text`：サブタイトル（任意）
4. 数分後、出力フォルダに候補画像（最大4位 × 2パターン＝最大8枚）が保存されます
5. 良いものを人が選んで、YouTube Studioからサムネイルとして設定してください

## ファイル構成

```
.
├── .github/workflows/generate-thumbnails.yml  # 実行トリガーと処理の定義
├── main.py               # 本体の処理（フレーム抽出→採点→合成→アップロード）
├── requirements.txt      # 必要なPythonパッケージ
└── README.md
```

## 調整できるパラメータ（main.py 上部）

- `FRAME_INTERVAL_SEC`：何秒おきにフレームを切り出すか（初期値5秒）
- `MAX_FRAMES`：切り出すフレームの上限（初期値40枚、APIコスト対策）
- `TOP_N`：採点上位いくつを候補にするか（初期値4）
