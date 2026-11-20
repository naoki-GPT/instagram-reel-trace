# 設計書（カットごとのYAML）の書き方

版4.0（2026-11-20）。最初の1コマを描く前に、カットごとに `cuts/cut-N.yaml` を書く。目的は、参考のコマを**商品まで含めて完全に再現する**こと。画像AIは商品を写さず描き直すため、言葉と数字で縛らないと「似た別物」になる（2026-11-20、箱2つが1本に合体した）。

## 原則

- **目で見た印象ではなく、測った数字で書く。** 位置は `work/src/grid-N.png`（10％ごとのマス目入り）で読み、画面の何％かで書く（x0, y0, x1, y1。左上が0）。色は参考のコマの画素から取った色番号で書く。
- **商品を一番先に、一番細かく書く。** 数・並び・境目・比率・持ち方・面ごとの絵柄の配置・主な色・印字の位置。
- 生成後の判定に使えるよう、`checks` は数えられる・見て分かる形で書く（「箱は2つで境目が中央」など）。
- 書き換えるのは人物と部屋だけ。商品・入れ物・机・構図は参考のまま。
- 参考店のロゴ・透かし・テロップは消す。**商品のパッケージの印字は残す。**

## 項目

| 項目 | 書くこと | 使われ方 |
|---|---|---|
| `cut` / `time` / `first_frame` | カット番号、参考での時刻、再現するコマの時刻 | 冒頭に人がいないカットは、人が入ったコマに `first_frame` をずらす（`project.yaml` の cuts と同じ値） |
| `camera` | 高さ・角度・レンズ（広角など）・どこで切れているか | 指示文 |
| `person` | 位置（％）・体の向き・手・視線・表情 | 指示文 |
| `products[]` | `id`・`what`・`count`・`layout`・`bbox_pct`・`held`・`art`（`colors`・`elements`・`title`）・`keep_print` | 指示文の先頭。`bbox_pct` から商品の拡大を切り抜き、3枚目の画像としてAIに渡す。生成後の数値チェックもこの範囲で行う |
| `containers[]` | 段ボール・ケースなど、商品以外で形を保つ物 | 指示文 |
| `room_map` | 参考の場所 → 新しい部屋の何にするか | 指示文（倉庫が残るのを防ぐ） |
| `remove` / `keep` | 消すもの・必ず残すもの | 指示文 |
| `extra` | そのカットだけの注意（例：左端の別人の腕を消す） | 指示文 |
| `talk` | 参考の人がカメラに向かって話しているカットは `true`（話す型）。書かなければ話さない型 | `true` なら本人がセリフを話す映像を注文し、組み立てでその声を使う。`false` なら口を閉じた動き＋選んだ声A/B/Cを重ねる |
| `motion` | `action`（動きを英語で）・`within_sec`（何秒で動き終えるか） | MiniMaxの動きの指示 |
| `use.in` | 4秒の生成映像の何秒目から使うか | 組み立て。**止まった部分ではなく動いている部分を選ぶ**（できた映像を見て決める） |
| `checks` | 判定の項目 | 生成後の確認 |

## 例（カット2・箱を2つ持って歩く）

```yaml
cut: 2
time: 2.10-4.17
first_frame: 2.15
camera: {height: chest, angle: front, lens: wide, crop: full body, notes: aisle between shelves, blue crate stacks at lower right}
person: {position_pct: [30, 28, 62, 95], body: facing camera mid-step, hands: both hands hold boxes at chest height, gaze: camera, expression: eyes smiling}
products:
- id: P1
  what: Japanese Pokemon Card Game booster boxes
  count: 2
  layout: two separate boxes side by side, edges touching at the center, NOT one long box
  bbox_pct: [24, 38, 64, 45]
  held: left box in left hand from below, right box in right hand from below, thumbs in front
  art:
    colors: ['#F2C230', '#E8574A', '#7FB7E6', '#D93A3A', '#3A6FD9']
    elements: 'left box: Pikachu-themed multicolor artwork with many Pikachu; right box: red, blue and white Pokemon artwork with Pikachu at the lower right'
    title: 'left: Japanese title at its right part; right: title near the center'
  keep_print: true
room_map: {left white shelves: black shop shelves full of booster boxes, back wall: the shop sign and the top shelves of the look image, right blue crate stacks: keep}
remove: [参考店のロゴ・透かし, 動画の上のテロップ]
motion: {action: he holds the two booster boxes at chest height and takes slow small steps toward the camera, within_sec: 2.0}
use: {in: 0.0}
checks: [two separate boxes, left box mostly yellow with Pikachu, right box red-blue-white]
```

## 色の測り方（例）

```python
from PIL import Image
im = Image.open('work/src/ref-2.png').convert('RGB')
box = im.crop((259, 730, 691, 864))          # bbox_pct を 1080x1920 の画素にしたもの
print(box.quantize(5).convert('RGB').getcolors())   # 多い順の色 -> '#RRGGBB' にして colors へ
```

## 渡す画像は3枚まで

1. 参考のコマ（`work/src/ref-N.png`）
2. 人物＋お店の画像（`project.yaml` の look）
3. 商品の拡大（そのカットの商品をすべて1枚にまとめたもの。`reel.py` が自動で作る）

売る商品の写真など、別の画像は足さない。画像を増やすほど破綻しやすい。MiniMax には描いた1コマの1枚だけを渡す。
