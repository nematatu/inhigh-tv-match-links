# インハイTV 試合リンク集

2026年インターハイ・バドミントンについて、BIRD SCOREの試合情報とインハイTV公式アーカイブを対応付ける、独立した拡張機能・静的リンク集です。

既存の `../inhigh-tv-skip-extension/` とは別のプロジェクトです。既存拡張のファイルは変更しません。

## 構成

- `extension/`: BIRD SCOREへ「動画を見る」を追加し、インハイTVで指定秒へ移動するManifest V3拡張
- `site/`: 日付・種目・回戦順に試合をまとめて表示する静的リンク集（学校名・結果・スコア付き）
- `tools/build-data.mjs`: BIRD SCORE公開JSONとインハイTV公開API/HLSから対応データを再生成
- `tools/crop-local-archives.py`: 外付けHDDの元動画を読み取り、試合開始秒ごとの動画を種目別に生成
- `data/`: 生成した対応データの正本

## データ生成

Node.js 18以降で次を実行します。

```bash
npm run build:data
```

生成処理は、BIRD SCOREの試合開始時刻と、インハイTVのHLSに含まれる `EXT-X-PROGRAM-DATE-TIME` を使って開始秒を計算します。署名付きHLS URLやAPIキーは生成データへ保存しません。

試合が未実施、開始時刻なし、アーカイブのメディアID不正などの場合は、リンクを有効化せず `unavailable` として記録します。推測で秒数を埋めません。

## 静的ページ

```bash
npm run serve
```

ブラウザで `http://localhost:8080/` を開きます。静的ページから公式アーカイブへ移動して指定秒へ自動移動するには、同梱拡張を導入してください。

## 拡張機能の導入

Chrome / Edgeで `chrome://extensions` または `edge://extensions` を開き、デベロッパーモードを有効にして `extension/` を「パッケージ化されていない拡張機能」として読み込みます。

拡張は次の公式ページだけを対象にします。

- `https://www.birdscore.live/web/interhigh-2026-wakayama-team/*`
- `https://www.birdscore.live/web/interhigh-2026-wakayama-Individual/*`
- `https://inhightv.sportsbull.jp/summer/archive/*`

Android Chromeの拡張機能対応やiOS Safari Web Extensionの配布は、この独立プロジェクトのコードとは別に端末ごとのパッケージ化が必要です。

## 動画リンクの扱い

リンク先はインハイTV公式アーカイブページです。動画ファイルやHLSプレイリストをこのプロジェクトから再配信しません。

## 外付けHDDの試合別動画

元の年月日・コート別アーカイブは変更せず、読み取り専用の入力として扱います。出力先に `MS/`、`MD/`、`WS/`、`WD/`、`TEAM-M/`、`TEAM-W/` を作り、BIRD SCOREで確認した開始秒から次の試合開始直前までをMP4として保存します。既存の出力先ファイルがあれば再利用するため、中断後も続きから実行できます。

```bash
python3 tools/crop-local-archives.py \
  --source "/Volumes/名称未設定/inhigh-tv-2026-badminton" \
  --target "/Volumes/名称未設定/inhigh-tv-2026-badminton-cropped"
```

処理を分割して並列実行する場合は、同じコマンドを `--workers 4 --worker 0` から `--worker 3` まで4本起動します。HDDの速度によっては並列数を増やしても速くならないため、1〜4本の範囲にしてください。

標準設定は映像を再エンコードせずに切り出すため高速で、元の画質を保ちます。フレーム単位の厳密な境界が必要な場合だけ `--reencode` を付けてください。元フォルダのファイルを削除・上書きする処理はありません。

別の保存先に十分な空き容量がある場合だけ、先に元フォルダを丸ごとコピーし、そのコピーを `--source` に指定できます。元フォルダと切り出し結果を同じ外付けHDDへ二重保存すると容量を大きく消費するため、通常は上記の出力専用フォルダを使います。
