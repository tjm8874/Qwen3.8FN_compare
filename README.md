# Qwen3.8-Flash-Next 実機比較

Qwen3.8-Flash-Nextを、現在購入できるPC・Mac・DGX Sparkで動かした際の速度とコーディング能力を測定します。各実機で使えるレシピを採用し、購入後の使い勝手を判断できるデータを残します。

**2026年10月10日現在、PCの3構成・Mac Studio・DGX Spark 1台・MS-S1 Max・MacBookの速度測定が完了しました。PRO 4500単独はDSHの25/25問、5060 Ti単独・GPU分割構成・Mac Studio・MS-S1 Maxは24/25問に成功しました。** 以下は途中経過です。著者の公表値や短い動作確認の数値を、今回の実測として扱いません。

## 16K入力では、PRO 4500単独がGPU分割より速かった

今回のStrata・IQ3_XXS・自動配置では、PRO 4500単独のDecode中央値は120.7 tps、PRO 4500＋5060 Tiは101.0 tpsでした。GPUを追加すれば単一リクエストが速くなるとは限らず、この条件で速度を重視するならPRO 4500単独が有力です。配置や条件を変えた場合、別の結論になる可能性があります。

Mac Studio M5 Ultraの既存8bitモデルはDecode中央値100.5 tpsでした。PCとはモデル・量子化・エンジンが異なり、Mac StudioのDSH評価は24/25問に成功し、reactが不成功でした。PCのGPU分割構成もDSHの24/25問に成功しました（不成功：react）。PRO 4500単独は25/25問に成功しました。5060 Ti単独は24/25問に成功しました（不成功：react）。価格や速度だけで全構成の購入順位を決めず、残りの25問評価と合わせて検討します。

<!-- RESULTS_START -->
| 構成 | Prefill tps | Decode tps | DSH 成功数 | 参考価格 USD |
|---|---:|---:|---:|---:|
| PC : PRO 4500 + 5060 Ti | 3,157.1 | 101.0 | 24/25 | $6,500 |
| PC : PRO 4500 32GB | 3,683.0 | 120.7 | 25/25 | $5,700 |
| PC : 5060 Ti 16GB | 1,333.1 | 60.2 | 24/25 | $1,800 |
| MacBook Pro M5 Max | 920.8 | 44.3 | 評価中 | $6,699 |
| DGX Spark 1台 | 2,620.8 | 53.4 | 中断中 | $6,950 |
| MS-S1 Max | 1,789.0 | 47.0 | 24/25 | $3,879 |
| Mac Studio M5 Ultra | 3,125.0 | 100.5 | 24/25 | $9,999 |
| GX10 + DGX Spark | 未測定 | 未測定 | 未確定 | $14,100 |
<!-- RESULTS_END -->

速度は5回の中央値です。最小値・最大値、TTFT、全試行の数値は[CSV](data/summary.csv)に収録しています。価格は所有者の参考価格と販売店の表示価格を区別し、DGX Sparkの公式表示価格は在庫なしの参考値です。[価格の出典・構成・確認日](data/prices.json)をご確認ください。

![実機比較の途中経過](images/comparison-progress.png)

## 比較する8構成

| 構成 | エンジン・モデル | 参考価格 USD | 状態 |
|---|---|---:|---|
| PC：PRO 4500＋5060 Ti | Strata 0.1.42／IQ3_XXS | $6,500* | 速度測定・DSH25完了 |
| PC：PRO 4500のみ | Strata 0.1.42／IQ3_XXS | $5,700* | 速度測定・DSH25完了 |
| PC：5060 Ti 16GBのみ | Strata 0.1.42／IQ3_XXS | $1,800* | 速度測定・DSH25完了 |
| MacBook Pro M5 Max 128GB・2TB | [MTPLX](https://github.com/youssofal/MTPLX)／Optimized Speed 4/8bit | $6,699* | 速度測定完了・DSH25評価中 |
| DGX Spark 1台 | [TensorFold指定レシピ](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold)／INT4・専門家削減モデル | $6,950† | 速度測定完了・DSHは16問後に中断 |
| MS-S1 Max 128GB・2TB | [Halogen指定レシピ](https://github.com/claudejaune/Qwen3.8-Halogen-Flash-Strix-Halo)／0.17.5・W4B＋品質オーバーレイ | $3,879* | 速度測定・DSH25完了 |
| Mac Studio M5 Ultra 256GB・2TB | 既存MLX-Serve／Uncensored 8bit | $9,999* | 速度測定・DSH25完了 |
| GX10＋DGX Spark | vLLM 0.29.0／Uncensored hybrid 4/8bit | $14,100* | 既存結果の来歴確認・16K速度の追加を準備中 |

\* 所有者が示した参考価格。PC本体はCore i5-14400・DDR4 64GB・Z790Aマザーボードを含めて$1,000、PRO 4500は$4,700、5060 Ti 16GBは$800で計算しています。販売店の確認価格は、確認日・SSDなどの構成・税送料の扱いとともに別途記録します。価格だけで能力を順位づけせず、速度と25問の成功数を併記します。

MacBook Pro M5 Max 128GB・2TBの$6,699は所有者が示した実売価格、MS-S1 Max 128GB・2TBは$3,879です。GX10＋DGX Sparkは、所有者の指定に合わせて1台あたり$6,950とし、接続ケーブル$200を加えた**$14,100**（$6,950 × 2 ＋ $200）で比較します。GX10単体について、販売店で確認した価格とは区別します。

† [NVIDIA US公式ページ](https://marketplace.nvidia.com/en-us/enterprise/personal-ai-supercomputers/dgx-spark/)の2026年10月10日の表示価格。128GB・4TB構成は在庫なしであり、この価格で今すぐ購入できることを示しません。

Mac Studioは**M5 Ultra・256GB・2TB、$9,999**が正しい構成です。稼働中のMLX-Serveは申告上の26.10.1に対し、バイナリが26.9.7-devと表示しました。既存サービスを変更せず、実際のバージョン・設定を生ログに保存しています。

## 16K入力の速度を測る

チャットの形式を含めて16,384入力トークン、2,048出力トークン、thinking OFF、同時実行1件で測定します。モデルを読み込み、同じ16K入力・2K出力で3回ウォームアップした後に5回実行し、入力処理（Prefill）と生成（Decode）の中央値・最小値・最大値を記録します。最初の出力が届くまでの時間（TTFT）は別の項目です。

入力の再利用は回避し、エンジンが報告する再利用数を確認します。入力長が違う実行、途中で回答を終えた実行、再利用が残った実行は、生ログを保存したうえで固定長の比較から除外します。速度を算出するトークン数の定義にエンジン間の差があれば、結果に明記します。

DGX SparkのTensorFoldは、Decodeの分子を「生成トークン数−1」、分母をエンジン内部の入力処理終了から生成終了までの時間としています。APIの総時間・Prefill時間・TTFTは計測区間が異なるため、これらの差からDecodeを再計算しません。[エンジンごとの計算定義](data/native-timing-definitions.json)と生ログを併記しています。

MS-S1 MaxはUbuntuのカーネル7.0.0-38、BIOSのUMA設定Auto、GPU用GTT上限120GiB、accelerator-performanceプロファイルで測定しました。Halogen 0.17.5のW4Bチェックポイントと品質オーバーレイを固定し、入力キャッシュを無効にしています。Prefill中央値は1,789.0 tps、Decode中央値は47.0 tps、TTFT中央値は9.17秒でした。HalogenのDecodeは生成トークン数全体を分子に使い、TensorFold・vLLMの「最初の1トークンを除く」定義との差を[計算定義](data/native-timing-definitions.json)に記録しています。IOMMU設定は変更していません。DSHの25問評価は24/25問に成功し、不成功はreactでした。25問を中断せず実行し、通信エラー・外側のタイムアウトはありませんでした。

MacBook Pro M5 Maxはクリーン再起動後、AC電源、MTPLX 2.12.2のturboプロファイル、MTP深度3、同時1件、GPUメモリ上限120GiBで測定しました。ファンはApple標準の自動制御です。Prefill中央値は920.8 tps、Decode中央値は44.3 tps、TTFT中央値は17.86秒でした。5回の本測定はPrefill 843.8〜1,157.6 tps、Decode 43.7〜54.2 tpsで、実行順に速度が低下しました。温度・ファン回転数を取得できていないため、原因は断定しません。MTPLXのネイティブtimingsは生成トークン数全体を分子とし、エンジンの総経過時間からPrefillとキャッシュ復元時間を引いた値を分母に使います。別の計測区間を使うmtplx_statsの値も生ログに保存しました。

## DSHでts-benchの25問を評価する

DSH 0.1.5-rc.3、同一のts-bench・Exercismコミット、[同じ25問](data/exercises.txt)を使用します。thinking ON、エージェントの文脈上限32K、1応答の出力上限16K、1問20分、逐次実行で条件をそろえます。temperature 1、top_p 0.95、top_k 20、seed 16を中継処理で指定し、実際に送った要求を保存します。成功数、所要時間、タイムアウト、ツールの失敗を分けて記録します。

ts-benchにはDSH用のアダプターを追加しています。固定コミットからの変更は[パッチ](data/harness.patch)、実行方法は[scripts/](scripts/)に収録しています。未変更のts-benchによる標準結果ではありません。

## モデル差を含めて、購入時の選択肢を比べる

PCの3行は同一IQ3_XXSモデルでGPU構成の違いを比較します。全8行では量子化、Uncensored版、モデルの削減、実行エンジンが異なります。DGX Spark 1台の削減モデルも採用し、「1台買った場合、このレシピは役立つか」を速度と評価結果から検討します。この比較から、ハードウェアだけの優劣を断定することはできません。

## データをたどれる形で公開する

公開用ログとスクリプトでは、LANアドレスと個人フォルダを`<PRIVATE_IP>`・`<HOME>`に置換しています。スクリプトを再実行する際は、自分の環境の値に設定してください。

- `data/protocol.json`：測定条件
- `data/targets.json`：実機・レシピ・価格の前提
- `data/summary.csv`：集計結果。未測定の値は空欄
- `data/prices.json`：参考価格の出典、構成、在庫・予約状況
- `data/raw/`：個別の計測値と、生ログをまとめた`raw.tar.gz`。要求、出力、SSE、設定、取得できたテレメトリを収録
- `images/`：Xで読みやすい比較表画像。現時点の画像には「途中経過」と明記

APIキー、実機のプライベートアドレス、シリアル番号、個人のファイルパスは公開データに含めません。生ログの公開版では必要な箇所を伏せ、原本と公開版のハッシュ、伏せ方を`archive-manifest.json`に記録します。短い出力による予備測定と入力長の調整用要求は、速度の集計から除外しています。Xへの最終投稿文は、測定結果がそろった段階で作成します。
