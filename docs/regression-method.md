# 비회귀 비교: 측정 가능한 것과 아직 모르는 것

“기존보다 정량적인 성능이 너프되면 안 됨”을 테스트 성공과 같은 말로 취급하지 않습니다. 이 도구는 **변경 전 결정적 Python 오프라인 구현과 변경 후 구현**만 같은 입력으로 비교합니다. 저장소의 더 오래된 수동/Grok/AI 작업 품질을 재현한 기준선이 아니며, 새 API·영상 기능의 정확성이나 사용자 Mac 성능을 대신 증명하지 않습니다.

## 기준선과 실행

변경 전 소스를 별도 읽기 전용 보관 위치에 먼저 복사합니다. 테스트를 위해 임의로 새 구현을 “old”라는 이름으로 복사하면 기준선 비교가 아닙니다. 이번 작업의 보존 기준은 확장 모듈 추가 전 Python 구현이며, 소스별 SHA-256 목록으로 정확한 내용을 식별합니다. 로컬 파일 목록만으로 Git 커밋의 진위를 증명하지 않습니다.

이미 만들어 둔 동일한 `examples/offline/generate.py` fixture를 양쪽에 입력합니다. 폰트·이미지·manifest를 새 버전별로 재생성하지 않습니다. 로컬 한국어 폰트가 필요합니다. 실제 상품, 실제 제휴 실적 또는 모델 생성물이 아닌 합성 도형 3개와 표지 1개입니다.

```bash
python tools/regression_compare.py \
  --baseline /absolute/path/to/preserved-baseline \
  --candidate /absolute/path/to/honeytem-cardnews \
  --manifest /absolute/path/to/unchanged-fixture/manifest.json \
  --runs 9 \
  --work-dir /absolute/path/to/new-empty-measurement-output \
  --report /absolute/path/to/regression-result.json
```

양쪽 소스는 신뢰할 수 있는 로컬 코드여야 합니다. 도구가 두 구현의 Python을 실행하므로 비신뢰 다운로드를 샌드박스로 격리하는 도구가 아닙니다. API 호출·로그인·링크 발급·유료 실행·게시를 하지 않습니다. 오프라인 경로만 비교합니다. `--work-dir`를 생략하면 결과물은 임시 디렉터리에서 검사 후 삭제되며 JSON 측정 기록만 보존됩니다. 지정하면 원본 결과물을 직접 검수할 수 있습니다.

## 내용 비교

- manifest, font, asset 및 런타임 버전 해시가 동일한지 확인합니다. 구현 코드 해시만 달라도 되는 항목입니다. 측정 전후 소스 및 매 실행 입력 해시 변경은 실패합니다.
- 각 버전의 반복 cold build에서 ZIP SHA-256이 모두 동일해야 합니다. 각 resume이 실제 재개이고 cold의 같은 패키지를 반환해야 합니다.
- 이미지: PNG/JPEG 원시 바이트와 디코딩한 크기·색 모드·**모든 RGBA 채널**을 비교합니다. 변경 픽셀 수, 변경 채널 수, 최대 차이, 경계를 기록합니다. RGBA의 `getbbox()` 기본 alpha-only 동작에 의존하지 않고 4개 채널의 차이를 합칩니다. 불투명 alpha가 같아도 RGB 변경을 잡고, alpha=0 아래의 RGB도 비교합니다.
- caption, links JSON, QA, 검수 문구, originals: 바이트 단위로 완전히 같아야 합니다. URL 재인코딩·중복 query 순서·QA 미완료 상태의 임의 PASS 변경을 허용하지 않습니다.
- provenance: 실제 관찰된 전체 input hash 및 fingerprint와 일치해야 합니다. 그 검증 후에만 `fingerprint`와 `input_hashes.code`를 제외해 비교합니다. 제품, 출처, 가격, 링크, 권리·확인 상태, 런타임, 다른 hash 항목은 제외하지 않습니다.
- package: 전체 멤버 집합·순서·바이트·해시, manifest의 정확한 필드·버전·hash map, 고정 ZIP 메타데이터를 별도로 확인합니다. 기대 파일로 ZIP을 독립 재구성해 정확히 같아야 하므로 덧붙인 숨은 바이트나 메타데이터 변조도 거부합니다.

새 소스 파일이 추가되면 fingerprint, provenance, package-manifest와 ZIP SHA가 바뀌는 것이 정상입니다. 이 경우 **ZIP 바이트가 같다고 쓰지 않습니다.** JSON의 `raw_equal`과 원시 SHA는 변경 사실을 보여주고, 허용된 차이만 정규화한 `equal`은 내용 보존을 별도로 보여줍니다. 새 기능의 영상을 옛 카드뉴스 패키지에 끼워 넣어 “동일 패키지”로 통과시키지 않습니다.

## 반복 성능 측정

기본 9회, 최소 5회에서 양쪽을 쌍으로 측정합니다. 순서는 baseline→candidate와 candidate→baseline을 교대로 실행합니다. 각 cold와 resume은 **별도의 새 Python 프로세스**입니다.

- `cold_build_seconds`: 빈 출력 디렉터리의 `build()` 호출 전체. 입력 검증·해싱·렌더·QA·패키징·동기화 쓰기를 포함합니다. Python import는 제외합니다. “cold”는 앱 캐시가 비었다는 뜻이며 OS 파일 캐시를 flush했다는 뜻이 아닙니다.
- `resume_build_seconds`: 같은 cold 산출물을 새 프로세스로 재개해 해시 무결성을 다시 확인하는 `build()` 호출 시간입니다.
- `*_peak_rss_bytes`: 각 새 프로세스의 build 완료 시 `ru_maxrss` high-water입니다. Linux KiB와 macOS bytes를 구분합니다. 다른 플랫폼은 지원하지 않는 측정으로 표시합니다. 인터프리터/import를 포함하며 단계별/증분 메모리가 아닙니다. 후속 verify 비용은 기록 이후입니다.
- `probe_process_seconds`: 프로세스 시작부터 후속 검증까지의 진단값으로, gated API 실행시간과 혼동하지 않습니다.

각 관측값, 중앙값·최솟값·최댓값, new/old 중앙값 비율, 쌍별 비율의 결정적 2,000회 bootstrap 95% 구간을 JSON에 저장합니다. bootstrap은 이 fixture와 이 실행 세션의 불확실성을 드러내는 기술 통계이며, 모든 입력·OS·부하 조건의 속도를 증명하지 않습니다. 호스트의 다른 테스트·ffmpeg 부하는 가능하면 종료하고 측정합니다. 샘플을 취사선택하지 않습니다.

기본 허용 저하율은 **0%**입니다. 중앙값이 증가하지 않고 bootstrap 상한도 1 이하이면 해당 metric은 `pass`, 중앙값이 증가하고 하한도 1 초과이면 `regression`, 그 밖의 측정 잡음/혼합 결과는 `inconclusive`입니다. 지원되지 않거나 부족한 측정은 `blocked`입니다. 불확실성을 임의 5–10% 허용치로 PASS 처리하지 않습니다. 시간 또는 메모리 중 하나라도 회귀면 오프라인 정량 gate는 회귀입니다. 원인을 고친 뒤 전체 쌍 비교를 다시 실행하고 앞선 결과도 보관해야 합니다.

## 머신 판독 gate와 종료 코드

JSON의 `gates`를 읽어 어느 범위가 통과했는지 구분합니다.

- `deterministic_offline_correctness`: 동일 입력의 이미지·카피·링크·QA·패키지 내용 보존과 negative controls.
- `observed_offline_quantitative`: 위 내용 보존과 cold/resume 시간·RSS의 측정 gate를 합칩니다. 일부 불확실성이 있으면 `inconclusive`이며 PASS가 아닙니다.
- `original_manual_or_ai_quality`: 같은 입력의 기존 모델 출력 및 평가 기준 부재로 `blocked`.
- `actual_product_semantic_fidelity`: 실제 상품의 승인된 정답 이미지 부재로 `blocked`.
- `user_mac_performance`: 사용자 Mac에서 비교하지 않았으므로 `blocked`.
- `live_provider_quality_cost_latency`: 승인된 실 API 비교가 없으므로 `blocked`.
- `overall_no_nerf`: 관찰된 회귀가 있으면 `regression`, 그렇지 않아도 위 미검증 범위가 남아 있으므로 `blocked`. 현재 도구로 전체 무너프를 PASS시킬 수 없습니다.

기본 `--gate overall`은 위 전체 기준을 사용합니다. 종료 코드 0=선택한 범위 PASS, 1=관찰된 회귀, 2=불확실/차단/실행 실패입니다. CI에서 **오프라인 범위만** 확인하려면 명시적으로 `--gate offline`을 사용하되, 전체 gate가 해소된 것으로 보고하지 마세요. `publish_ready`는 항상 false입니다.

## 거짓 양성을 막는 검사

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_regression_compare.py' -v
```

RGB 단일 채널 변경, alpha 단독 변경, 투명 영역 RGB 변경, 이미지 크기·모드 변경, caption 변조, 정확한 링크 인코딩 변경, QA gate 업그레이드, 파일 추가/누락, ZIP 멤버 변조/덧붙임, provenance 거짓말, 다른 런타임을 모두 검사합니다. 변경 코드만 있는 실제 두 소스 트리의 5회 비교도 실행하며, 내용은 같아도 원시 ZIP SHA는 다르고 전체 gate는 PASS가 아니어야 합니다. 의도적 결함을 탐지하는 negative control과 양성 동일성 검사를 모두 수행합니다.

## 남은 평가

현재 도구는 새 기능이 기존 결정적 카드뉴스 경로를 훼손하는지를 검출하는 안전장치입니다. 원래 모델 대비 의미·미학 품질, 실제 상품 정확성, 공급자 비용/지연, 영상 화질·길이·파일 크기, 사용자 Mac 처리량은 각각 같은 입력·동등 설정·반복 샘플·명시된 승인 기준으로 별도 비교가 필요합니다. 기존 출력/런타임을 확보하지 못한 항목은 UNKNOWN/BLOCKED로 유지하고 “테스트 전부 통과”로 덮지 않습니다.

## 캔버스 수명 수정 후 비교의 관찰값 (2026-10-21)

동일 Linux/Python 3.12.14/Pillow 12.3.0/fonttools 4.61.1에서 교대 순서 15쌍, 즉 cold 30회와 새 프로세스 resume 30회를 측정했습니다. 소스 해시는 측정 전후 불변이었고, 각 버전 내 반복 ZIP은 모두 재현됐습니다. 이전 결과와 같은 기준선·fixture·측정 도구·0% gate를 사용했습니다.

측정 이력도 보존합니다. 첫 15쌍 뒤 provider/reel 안전 검사 코드가 변경되어 두 번째 비교를 했습니다. 두 번째 비교는 cold RSS +8,192 bytes의 엄격한 회귀를 발견했습니다. 이에 렌더링된 PNG/QA를 저장한 뒤 페이지의 이미지 메모리를 명시적으로 닫고 해제하는 수정을 적용했습니다. 다음 페이지 할당과 이전 캔버스가 겹치는 수명을 줄이며, 10개 전체 변형에서 PNG 바이트 및 전체 QA가 기준선과 같음을 별도로 확인한 뒤 세 번째 비교를 했습니다. 이전 관측값을 삭제하거나 허용 저하율을 넓히지 않았습니다.

- 기준 코드 트리 SHA-256: `f550f6f502fb9faeeb8cac63169ebecb29d1db4c72b2ef4bf223214104950c93`
- 현재 비교 코드 트리 SHA-256: `df67224737c8efc9913db9cb238b96b8a7c4a4f34bdbf9bd9716505a70301717`
- 4개 카드 + 3개 원본: 변경 픽셀/채널 0. caption/links/QA/검수 문구/원본 바이트 동일. provenance 구현 식별 정보만 다름.
- 렌더 PNG 총량 변화 0 bytes. ZIP은 구현 해시 목록 증가로 276,217→276,377 bytes (+160 bytes). 전체 ZIP 동일성을 주장하지 않음.
- 현재 ZIP SHA-256: `881d24fffcfd2ec30ba41c1106e00afb3d52516087150655708c6a9f5ec63f7c`
- Cold build 중앙값: 0.709194→0.744502초 (**+4.98%**). 쌍별 bootstrap 구간 0.96800–1.07248. 중앙값 증가를 숨기지 않으며, 구간이 1을 포함해 gate `inconclusive`입니다. 속도 비회귀 PASS로 해석할 수 없습니다.
- Resume build 중앙값: 0.066034→0.063089초. 비율 0.95540, 구간 0.91239–0.97671. 이 세션의 gate `pass`.
- Cold peak RSS 중앙값: 347,230,208→344,662,016 bytes (**−2,568,192 bytes**). 비율 0.9926038, 구간 0.9925921–0.9926156. 이전에 발견된 cold RSS 회귀를 수정했고 이번 세션의 gate `pass`.
- Resume peak RSS 중앙값: 60,764,160→60,768,256 bytes (**+4,096 bytes**). 비율 1.0000674, 구간 0.9999326–1.0000674. 0% 저하 기준의 gate `inconclusive`이며 작은 증가를 통과로 반올림하지 않습니다.

따라서 **결정적 내용 보존 PASS / 오프라인 정량 INCONCLUSIVE / 전체 무너프 BLOCKED**입니다. cold 시간과 resume RSS의 추가 근거 없이 전체 정량 비회귀를 완료했다고 보고할 수 없습니다. 원래 AI 의미 품질·실제 상품·사용자 Mac·실 API 평가도 별도로 BLOCKED입니다. 모든 머신에서 같은 결과가 나는지 증명하지 않습니다.

위 수치는 옛 단일 벤치마크 프로세스의 누적 high-water RSS와 비교할 수 없습니다. 수치가 더 작다는 이유로 그 과거 측정 대비 메모리 최적화를 주장하지 않습니다. 비교 코드가 바뀌면 이 결과를 그대로 그 새 버전에 적용하지 말고 다시 측정해야 합니다. 원시 JSON에는 60개 probe의 원시 시간/RSS, 소스·입력·패키지 해시와 gate가 보존됩니다. 현재 기록은 `comparison-15-pairs-canvas-release.json`, 이전 기록은 `comparison-15-pairs-v1.json` 및 `comparison-15-pairs-final.json`입니다. 마지막 이름의 `final`은 그 당시의 이름일 뿐이며 현재 소스에 적용되는 기록은 아닙니다.
