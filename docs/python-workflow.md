# 오프라인 Python 카드뉴스 워크플로

이 구현은 `honeytem-cardnews`만을 위한 **판매 전 검토용** 도구입니다. 기존 문서의 제휴·디자인 규칙을 바꾸지 않습니다. 기본 validate/build/tasks/import-result 경로는 네트워크를 사용하지 않습니다. 선택형 execute-provider만 명시적 허용 하에 설정한 모델 서버를 호출합니다. 로그인, 가입, 제휴 링크 발급, 자동 웹 수집, 게시·예약 발행, 서비스 설치는 하지 않습니다. 외부 API 비용은 별도 확인 대상입니다.

## 무엇을 코드로 옮겼는가

- 상품별 번호·입력 계약 검증, 증거/권리/가격/조건/확인 시각 누락 차단
- 입력한 원본·제휴 URL을 그대로 유지하는 링크 목록
- 정확한 입력 사실만 사용하는 한국어 캡션과 첫 줄 대가성 문구
- 1080×1350 매거진형 PNG, 10가지 명시적 레이아웃 변형
- 원본 바이트 보관, 원본 해상도 그대로의 이미지 배치, 텍스트 넘침 차단
- 입력·소스코드·이미지·폰트·런타임 지문, 단계별 재개, 해시 검수, 재현 가능한 ZIP
- 정확한 ZIP SHA-256에만 연결되는 운영자 검토 기록

상품 조사·최신 정책 판단·설득력 있는 카피·AI 이미지 생성·미학/의미 검수는 별도 작업입니다. `tasks`는 구조화된 요청을 내보내고 `import-result`는 검토된 구조화 결과를 검증할 뿐, 모델을 실행하지 않습니다. 테스트 도형을 실제 모델 결과라고 주장하지 않습니다.

Python을 택한 이유는 이미지/한글 폰트 도구, 입력 검증, 유지보수 비용입니다. Rust보다 빠르다는 주장은 하지 않습니다. 원본 저장소에는 실행 가능한 렌더러나 비교 벤치마크가 없으므로 이번 측정은 **최초 기준선**입니다. 기존 AI 작업보다 성능/품질이 향상되었다는 증거는 아직 없습니다.

## 설치와 완전 오프라인 실행

Python 3.11 이상, Pillow 12.3.0, fonttools 4.61.1이 필요합니다. 설치가 필요한 경우 운영자가 자신의 환경에서 실행합니다. 빌드 자체는 네트워크를 사용하지 않습니다.

```sh
python -m pip install -e .
# 또는 의존성이 이미 설치되어 있다면 설치 없이:
export PYTHONPATH=src

python examples/offline/generate.py \
  --regular-font /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
  --bold-font /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
  --destination work/offline-input

python -m honeytem validate work/offline-input/manifest.json
python -m honeytem build work/offline-input/manifest.json --output output/offline
```

자신의 운영체제에 **실제로 존재하는 한글 폰트 경로**를 지정합니다. TTC 얼굴 번호는 `--regular-index`, `--bold-index`로 명시할 수 있습니다. 폰트가 없거나 글리프가 부족하면 기본 폰트로 대체하지 않고 실패합니다. fixture 생성기는 로컬 폰트를 복사하지만 저장소/완성 ZIP에는 폰트를 배포하지 않습니다. 폰트 이용 조건도 확인하세요.

fixture는 600×420 RGB 기하학 도형 세 장입니다. 모든 이미지에 합성 테스트임이 표시됩니다. 실제 상품·가격·추천인 코드·AI 생성 이미지가 아닙니다. 실제 판매 모드를 통과시키기 위해 fixture의 kind나 확인 플래그를 사실과 다르게 바꾸면 안 됩니다.

## 입력 계약

실행 가능한 완전한 예시는 위 생성기가 만든 `manifest.json`입니다. 정확한 필드 집합은 `src/honeytem/schema.py`가 단일 기준이며, 알 수 없는 필드도 거부합니다. JSON 중복 키와 NaN/Infinity, 정수 자리의 불리언도 거부합니다.

최상위:

| 필드 | 값/의미 |
|---|---|
| schema_version | 정수 1 |
| mode | `concept` 또는 `affiliate` |
| fixture | 합성 테스트 여부. affiliate에서는 false |
| edition, title, subtitle | 실제 제공한 문구, 렌더링 폭을 넘으면 실패 |
| variant | 정수 0–9, 순환 선택은 운영자가 명시 |
| checked_at | 시간대가 포함된 ISO-8601 검토 시각, 미래 불가 |
| fonts | regular와 bold 각각 path, index |
| channel | name=`instagram`, status, evidence_url, profile_links_confirmed |
| disclosures | 사용한 프로그램마다 최신 확인 문구·출처·시각 |
| products | 1–5개, ID는 `01`부터 연속 |

상품마다 `id`, `title`, `comment`, `program`, `seller`, `option`, `quantity`, `price`, `original_url`, `affiliate_url`, `evidence`, `asset`가 모두 필요합니다.

- 실제 판매: program은 `toss_sharelink`, `coupang_partners`, `kakao_gift_curator` 중 하나. 판매자·정확한 옵션·수량·가격·조건·배송 조건·원본 URL·본인이 발급한 제휴 URL이 필요합니다.
- price는 amount(0 이상 정수 원), currency=`KRW`, conditions, shipping. 상품 화면에서 확인한 정확한 가격만 기록합니다. 조건부 혜택가는 조건을 빠뜨리지 않습니다.
- evidence는 url, checked_at, claims_verified=true, link_ownership_confirmed=true. 이 플래그는 **운영자의 확인 진술**이며 도구가 링크 소유나 실적을 독립 검증했다는 뜻이 아닙니다.
- 실제 판매 channel은 status=`confirmed`와 evidence_url이 필요합니다. profile_links_confirmed=true인 경우만 프로필 링크 이동 문구를 씁니다. 계정에서 링크를 실제 등록했는지 별도로 확인합니다.
- concept는 program/seller/option/quantity/price/original_url/affiliate_url/evidence를 모두 null로 둡니다. 판매용 링크나 가짜 가격은 허용하지 않습니다. channel은 status=`not_applicable`, evidence_url=null, profile_links_confirmed=false이고 disclosures는 빈 목록입니다.

### 이미지 계약: 권리가 디자인보다 우선

asset는 path, kind, source, license, usage_confirmed=true, layout_permission_confirmed=true입니다. kind는 실제 판매=`actual`, 실제 AI 콘셉트=`ai_concept`, 코드 생성 fixture=`synthetic_fixture`로 구분합니다. 경로는 manifest 디렉터리 안의 상대 경로이고 symlink/상위 경로 탈출은 금지합니다. 소스는 실제 출처, license는 허용 범위/근거를 적습니다.

입력은 단일 프레임, EXIF 회전 없는 무색상프로필 RGB PNG/JPEG, **800×560 이하**여야 합니다. 확대·축소·자르기·색보정·배경 제거·번역·텍스트 오버레이를 원본 이미지 영역에 적용하지 않습니다. ICC/gamma/chromaticity/sRGB 태그 또는 비-sRGB EXIF 색공간은 검증된 색 관리 경로가 없어 거부합니다. 원본 프로필을 제거해서 통과시키라는 뜻이 아닙니다. 큰 이미지는 자동으로 줄이지 않습니다. 허용된 다른 소재나 별도로 권리가 확인된 적정 크기의 파일을 공급해야 합니다. 큰 원본을 임의 편집하라는 뜻이 아닙니다.

특히 토스 제공 이미지의 바깥 프레임 배치가 개별 소재와 사용 방식에서 허용되는지 확인해야 합니다. `layout_permission_confirmed`는 토스의 일괄 승인이 아닙니다. 원본을 편집할 수 없으므로 장식·광고·제목은 이미지 영역 밖에만 배치합니다. 원본 파일 자체는 ZIP의 `originals/`에 그대로 보관합니다. 원본 메타데이터도 보존되므로 공유 전에 위치/개인정보/권리를 점검하세요.

### 대가성 문구

disclosures 항목은 program, text, reference_url, checked_at, verified=true입니다. 실제로 사용하는 각 프로그램을 정확히 한 번씩 넣고 문구는 `[광고] `로 시작하며 해당 프로그램명과 수수료 관계를 명시합니다. 최신 공식 문구를 **원문 그대로** 넣습니다. 프로그램별 수익률·정산 규칙은 코드에 고정하지 않았습니다.

캡션 맨 앞과 첫 카드의 흰색 고대비 영역에 문구를 표시합니다. 다른 상품 카드에도 광고 표시를 둡니다. 혼합 프로그램은 모두 표시합니다. 협찬·별도 광고비 같은 추가 관계는 별도 검토가 필요하며 수수료 문구만으로 충분하다고 가정하지 않습니다. 도구가 최신 법률·계정 정책을 온라인 검증하지 않으므로 게시 전 최신 자료와 대조해야 합니다.

### URL·텍스트 안전

URL은 공개 HTTPS 형태만 받으며, credentials, 명백한 로컬 주소, 비밀 query 키, 공백/제어문자를 거부합니다. **URL을 요청하거나 리디렉션을 따라가지 않습니다.** 문자열의 순서, percent encoding, 중복 query, 추적 값은 한 글자도 고치지 않습니다. `.invalid`와 테스트 도메인 등은 판매 입력에 쓰지 않습니다. 도메인 형태만으로 해당 계정/제휴 자격/실제 링크를 보증하지 않습니다.

비밀번호·인증 토큰·계좌·신분증·세금 자료를 입력하지 마세요. 일반 오류는 입력 값을 출력하지 않습니다. 허용된 제휴 추적 링크는 게시물의 필수 자료이므로 링크/출처 파일에 보존됩니다. 출력 ZIP과 실제 상품 입력은 공개 저장소에 커밋하지 마세요.

## 단계, 재개, 승인 대상

```sh
python -m honeytem build work/offline-input/manifest.json --output output/offline --stop-after validated
python -m honeytem build work/offline-input/manifest.json --output output/offline --stop-after rendered
python -m honeytem build work/offline-input/manifest.json --output output/offline
python -m honeytem verify output/offline/FINGERPRINT --expected-sha256 EXACT_ZIP_SHA256
```

`validate → render → package`는 지문별 디렉터리에서 수행합니다. 입력·코드·이미지·폰트·Python/Pillow/fonttools 버전 변경은 새 지문으로 분리되며 과거 승인을 재사용하지 않습니다. 완료된 단계는 모든 파일 해시를 확인한 후 재사용합니다. 변조가 있으면 덮어써서 숨기지 않고 실패합니다. 렌더 디렉터리를 옮긴 직후 중단된 경우에만 재계산한 완전 동일 내용으로 체크포인트를 복구합니다.

원자적 임시 파일 쓰기/교체와 출력 루트의 배타 잠금을 사용합니다. 비정상 종료 후 `.honeytem.lock`가 남으면 실행 중인 프로세스가 없는지 운영자가 확인한 뒤 제거합니다. 자동으로 다른 작업의 잠금을 뺏지 않습니다. 공격자가 동시에 로컬 파일을 변경할 수 있는 적대적 다중 사용자 환경에 대한 완전한 TOCTOU 방어/암호학적 서명 서비스는 아닙니다.

결과:

- `rendered/cover.png`, `product-01.png` 등
- `caption.txt`, `links.json`, `originals/`
- `provenance.json`: 출처·권리·제품 증거·입력 해시
- `qa.json`: 자동 검사와 별도의 미완료 품질/정책/실상품/Mac 검증 항목
- `package-manifest.json`, `package.zip`: 정렬·고정 시간 ZIP
- `state.json`: 단계별 무결성 체크포인트

```sh
# 모든 내용 검토 후 운영자가 명시적으로 기록할 때만:
python -m honeytem record-approval output/offline/FINGERPRINT \
  --package-sha256 EXACT_ZIP_SHA256 --reviewer '운영자 이름'
```

검토 기록은 정확한 ZIP 바이트에만 연결됩니다. 인증된 전자서명이나 게시 권한이 아니며 게시 기능도 없습니다. `publish_ready`는 항상 false입니다. 승인 전후 파일이 달라지면 무결성 확인에 실패합니다. 외부에서 받은 신뢰된 SHA-256을 `verify --expected-sha256`에 넣으면 파일과 내부 manifest를 함께 바꾼 경우도 탐지할 수 있습니다. 내부 해시 파일만으로 악의적인 전체 교체를 인증할 수는 없습니다.

## 모델/조사 어댑터

```sh
python -m honeytem tasks work/offline-input/manifest.json > work/model-tasks.json
python -m honeytem import-result work/offline-input/manifest.json work/result.json \
  --output work/offline-input/revised.json
```

작업 파일 생성은 외부 서비스에 업로드하거나 모델/API를 실행하지 않습니다. 선택형 `execute-provider` 연결은 [제공자 안내](providers.md)를 따릅니다. 모델/권한/비용 확인과 네트워크 명시적 허용이 필요하며 결과는 미검토 후보로만 저장됩니다. 실제 상품 정보조사·카피·이미지는 각각 결과가 필요합니다.

결과 envelope: schema_version=1, input_fingerprint, product_id, kind, reviewed=true, output. kind는 product_research/editorial_copy/authorized_image입니다.

- product_research: output은 전체 상품 객체. ID와 원본/제휴 링크 변경 불가.
- editorial_copy: output은 title/comment/evidence_urls. 판매 카피는 현재 product.evidence.url 한 개를 원문 그대로 사용합니다. 다른 근거를 쓰려면 먼저 research 결과로 evidence를 갱신합니다. 콘셉트는 빈 evidence_urls를 사용합니다. 문체/사용 경험/품질 표현의 진실성은 운영자가 검토합니다.
- authorized_image: output은 전체 asset 객체. 실제 상품을 AI 유사 이미지로 바꿀 수 없습니다.

새 manifest는 같은 디렉터리의 새 파일로 저장하고 전체 검증을 다시 거칩니다. 낡은 입력 지문에 대한 결과는 거부합니다. 아직 상품 증거가 전혀 없다면 검증된 판매 manifest를 먼저 만들 수 없으므로 조사 담당자가 이 입력 계약을 따라 초안을 수동 준비해야 합니다.

## 검증과 비회귀 범위

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python tools/benchmark.py --help
```

테스트는 필수 값, 가격/증거, 번호/링크, 추적 문자열, 원본 픽셀/바이트, 글리프, 레이아웃 경계, 텍스트 넘침, 10변형, 재현성, 재개, 변조, 승인 대상 변경을 검사합니다.

수학적 불변식의 조건과 한계는 [검증 근거](verification.md)에 있습니다. 실제 모델 출력/상품 사진/기존 성능 기준선/사용자 승인 디자인 기준이 없어 **그 영역의 no-regression은 증명되지 않았습니다**. 사용자에게 보이는 테스트 산출물은 기능 확인 자료이지 승인된 골든 디자인이나 실판매 자료가 아닙니다. 검증되지 않은 코드를 main에 승격하지 말고 검토 브랜치에서 확인하세요.

## 선택 확장

- [실제 HTTP 모델 제공자 연결](providers.md): 기본 비활성화, 명시적 네트워크 허용, 미검토 후보 저장. `build`는 호출하지 않습니다.
- [무음 릴스 내보내기](reels.md): 검증된 카드 묶음에서 원본 카드 크기를 보존해 MP4를 생성합니다. 음악·TTS·게시를 자동으로 추가하지 않습니다.
- [기존/변경 비회귀 비교](regression-method.md)와 [운영 전환 조건](release-gates.md): 실제 상품·모델·Mac 영역은 별도 검증해야 합니다.
