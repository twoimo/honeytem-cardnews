# 명시적 선택형 AI 제공자 실행

`tasks`는 여전히 실행하지 않는 작업 파일입니다. 새 `execute-provider`는 운영자가 이미 사용할 수 있는 **로컬 HTTP 또는 호환 원격 HTTPS AI 엔드포인트**를 명시적으로 선택했을 때만 실제 POST를 수행합니다. 기본 `build`, `validate`, `tasks`, `import-result`에는 네트워크가 추가되지 않습니다.

구현은 Python 표준 라이브러리 HTTP/JSON/TLS와 기존 Pillow를 사용합니다. SDK, 서버, 모델을 설치하거나 다운로드하지 않고, 계정/키를 만들거나 권한을 확대하지 않습니다. 이번 검증에서는 루프백 HTTP 계약 fixture만 실행했고 실제 모델, 유료 API, 사용자 계정은 사용하지 않았습니다.

## 지원 범위와 보수적인 경계

- `editorial_copy`: 제공한 사실과 선택적 증거 발췌를 바탕으로 한국어 제목/코멘트를 생성합니다.
- `product_research`: 제공한 증거 발췌를 기존 상품 사실과 대조해 수정 후보를 만듭니다. **웹 검색·페이지 수집·가격 확인을 한 것처럼 주장하지 않습니다.** 현재는 이미 검증 가능한 affiliate manifest가 필요합니다. 처음 보는 상품을 빈 입력에서 조사하는 cold start, 다른 근거 URL로 전환, 최신 원문 수집은 이 명령이 해결하지 않습니다.
- `authorized_image`: `fixture=false`인 concept 입력에 한해 이미지 생성 엔드포인트를 호출합니다. 실제 판매 상품을 닮은 가짜 사진으로 바꾸거나 synthetic fixture를 실제 모델 결과로 바꾸어 표시하지 않습니다. affiliate 및 synthetic-fixture 입력에서는 호출 전에 실패합니다.
- 어떤 결과도 게시·예약 발행, 자동 승인, 제휴 링크 발급으로 이어지지 않습니다. 반환 envelope의 `reviewed`는 항상 `false`입니다.

`--allow-network`는 지정 엔드포인트로 요청할 기술적 선택입니다. 비용·개인정보 전송·이용약관·이미지 권리·게시 승인 자체가 아닙니다. 운영자는 수신 제공자, 전송할 자료, 계정의 비용/권한을 먼저 확인해야 합니다. 루프백 주소의 서버도 뒤에서 유료 원격 모델을 호출할 수 있습니다. 로컬 주소만으로 오프라인 추론이나 무료 사용을 보장하지 않습니다.

## 설정

로컬 텍스트 제공자용 JSON 예시입니다. `model`은 본인이 이미 사용하는 정확한 모델 식별자로 바꿔야 하며, 예시 문자열에 해당하는 모델이 있다고 가정하지 않습니다.

```json
{
  "protocol": "chat_completions",
  "base_url": "http://127.0.0.1:11434/v1",
  "model": "REPLACE_WITH_YOUR_EXISTING_MODEL_ID",
  "token_parameter": "max_tokens",
  "max_output_tokens": 2048,
  "json_mode": true,
  "timeout_seconds": 30,
  "max_attempts": 1,
  "max_request_bytes": 262144,
  "max_response_bytes": 25165824
}
```

필수 필드는 `protocol`, `base_url`, `model`입니다. 나머지는 예시 값이 기본입니다. 허용하지 않은 설정 키는 실패합니다.

- `protocol`: `chat_completions` 또는 `images_generations`. 자동으로 다른 프로토콜/모델로 대체하지 않습니다.
- `base_url`: API 기본 경로입니다. 각각 `/chat/completions` 또는 `/images/generations`를 붙입니다. 사용자정보, query, fragment, 경로의 `..`, percent encoding은 거부합니다. `http`는 명시적인 루프백에만 허용하며 `localhost`는 DNS 없이 `127.0.0.1`로 고정합니다. 원격은 HTTPS와 `--allow-remote`가 모두 필요합니다.
- `model`: 운영자가 지정한 ID를 그대로 전송합니다. 서버가 그 모델을 실제 실행했는지는 독립 검증하지 못합니다.
- `token_parameter`: 서버가 지원하는 `max_tokens` 또는 `max_completion_tokens`를 선택합니다. `json_mode=false`는 JSON 응답 형식 요청을 생략할 뿐, 반환 JSON 검증을 끄지 않습니다.
- `api_key_env`: 필요할 때 이미 준비된 런타임 환경 변수의 **이름만** 지정합니다. 예: `HONEYTEM_EXISTING_PROVIDER_KEY`. 값, 인라인 `api_key`, 임의 Authorization 헤더, `.env` 자동 탐색/저장은 지원하지 않습니다. 코드는 계정 인증이나 키 발급을 대신하지 않습니다.
- `max_output_tokens`: 1–32768. 서버에 요청하는 출력 한도이며, 서버가 보고한 초과 사용량도 오류 처리합니다. 입력 토큰/이미지 가격/서버 과금까지 포함한 금전 한도는 아닙니다.
- `timeout_seconds`: 0.05–300초. DNS 조회 후 연결·TLS·응답 헤더/본문에 적용되는 요청 시도별 제한입니다. 느리게 조금씩 데이터를 보내는 서버도 socket 종료 watchdog으로 차단합니다. OS DNS resolver의 시간은 이 제한에 포함되지 않으며 OS 설정을 따릅니다.
- `max_attempts`: 1–3, 기본 1. 2 이상을 명시했을 때만 HTTP 429 거절을 제한적으로 다시 시도합니다. Retry-After 대기는 최대 5초입니다. 타임아웃, 연결 중단, 5xx, 잘못된 응답, 모델 거절은 자동 재시도하지 않습니다. 성공/과금 여부가 불확실한 요청을 임의로 다시 보내지 마세요.
- `max_request_bytes`: 1024–2097152바이트. `max_response_bytes`: 1024–33554432바이트. gzip/압축 응답은 받지 않습니다.

이미지 제공자는 `protocol`을 `images_generations`로 설정합니다. 선택적 `image_options`에는 해당 서버와 모델이 **실제로 지원하는** `size`, `quality`, `response_format`, `output_format`, `background`만 넣을 수 있습니다. `response_format`을 지정하면 `b64_json`만, `output_format`을 지정하면 `png` 또는 `jpeg`만 허용합니다. 지원하지 않는 크기나 파라미터를 추측하지 마세요. 기본 이미지 크기/품질은 제공자가 정하며, 그 값이나 지원 여부를 코드가 보장하지 않습니다.

인증 없는 로컬 서버는 `api_key_env`를 생략합니다. 원격 API를 쓰기 위한 키의 구매·발급·등록 과정과 실제 호출은 이번 구현 검증에 포함되지 않았습니다.

## 입력 자료와 실행

카피에서 추가 발췌가 없으면 source-material 파일 내용은 `[]`입니다. 판매 상품의 조사 입력은 다음 구조의 배열이며, `url`은 해당 상품에 이미 보관된 `evidence.url`과 정확히 같아야 합니다. 예시 문구를 실제 근거로 사용하지 마세요.

```json
[
  {
    "url": "https://shop.vendor.co.kr/evidence/01",
    "excerpt": "실제로 확인하고 제공할 권한이 있는 상품 원문의 발췌를 여기에 넣습니다."
  }
]
```

자료는 최대 8개, 각 발췌는 최대 32000자이며 요청 전체 바이트 제한도 적용됩니다. URL을 방문하지 않습니다. 증거 URL·발췌·제목·코멘트·판매자·옵션·수량·가격은 선택한 제공자에게 전달됩니다. 제휴 추적 링크, 로컬 이미지/폰트 경로, 소유/이용 승인 플래그는 텍스트 모델 요청에 넣지 않습니다. 비밀번호·키·개인/계정 민감 자료를 발췌나 프롬프트에 넣지 마세요.

```sh
PYTHONPATH=src python -m honeytem execute-provider \
  work/input/manifest.json 01 editorial_copy \
  --config work/provider.json \
  --source-material work/source-material.json \
  --output work/copy-proposal-01 \
  --allow-network
```

실제 판매 상품의 근거 대조는 같은 명령의 `editorial_copy`를 `product_research`로 바꿉니다. 원격 호출에는 추가로 `--allow-remote`가 필요하지만, 제공자에 자료를 보내도 되는지와 비용 승인은 별도로 확인해야 합니다.

이미지 source-material의 형태는 `{"prompt":"운영자가 검토한 콘셉트 이미지 요청"}`입니다. `authorized_image`와 이미지 protocol 설정을 사용합니다. 입력 manifest를 통과시키려고 도형 fixture의 `fixture`/`kind`를 사실과 다르게 바꾸면 안 됩니다. 기존 권리 확인된 비-fixture 콘셉트 입력이 없으면 먼저 올바른 입력을 준비해야 합니다.

## 결과와 인간 검토

새 출력 폴더에만 저장합니다. 기존 폴더를 지정하면 호출 전에 실패하므로 같은 시도를 자동 중복 실행하지 않습니다.

- `attempt.json`: 시작/완료/실패와 검토되지 않았다는 상태. 프로세스가 중단되어 `started`로 남았다면 성공 여부를 확인하기 전 재호출하지 않습니다.
- `candidate.json`: 기존 어댑터 envelope와 호환되지만 `reviewed=false`여서 바로 import할 수 없습니다.
- `trace.json`: 입력 지문, 요청/응답/결과 SHA-256, 설정 모델/엔드포인트의 SHA-256, 시각, 바이트/시도 수, 제공자가 보고한 제한된 사용량. 실제 키, 전체 엔드포인트, 모델명, 프롬프트, 응답 본문, HTTP 오류 본문은 기록하지 않습니다. 설정 파일을 로컬에 보관해야 해시와 선택한 제공자를 나중에 대조할 수 있습니다. 해시는 익명화·암호화·서버 진위 증명이 아닙니다.
- 이미지 작업에는 응답의 원래 PNG/JPEG 파일. URL 형식 이미지는 다시 내려받지 않고 실패합니다.

조사 후보는 기존 ID/원본 URL/발급된 제휴 URL/이미지·권리 기록을 그대로 유지하고, 새 사실에 대해 `evidence.claims_verified=false`로 내립니다. 사람은 원문·시각·상품 옵션·가격·조건을 다시 확인한 뒤에만 실제 확인 사실을 기록해야 합니다. `reviewed`만 true로 바꾸어도 이 확인이 빠지면 import는 실패합니다.

카피 후보도 사람의 사실/문체 검토가 필요합니다. 모델이 근거를 달았다고 내용의 진실성을 증명한 것은 아닙니다. 새 증거 URL을 사용하려면 기존 입력과 증거 절차부터 갱신해야 합니다.

이미지 후보의 `usage_confirmed`, `layout_permission_confirmed`는 모두 false이고 license도 검토 요청일 뿐 허가가 아닙니다. 실제 제공자/모델 이용 조건, 생성물 권리, 콘셉트 표시, 레이아웃 허용을 사람이 확인해야 합니다. 이미지 파일을 manifest 입력 폴더에 보관하거나 그 안의 적절한 상대 경로로 지정하고 전체 입력 검증을 다시 거칩니다.

이미지를 조용히 축소·크롭·재인코딩하지 않습니다. 네이티브 파일이 800×560/RGB/프로필 등 기존 asset 계약에 맞지 않으면 `trace.image.existing_asset_contract_compatible=false`입니다. 이미지는 검토용으로 그대로 보관하지만 자동 import 가능한 것으로 표시하지 않습니다. 적법하게 사용할 수 있는 다른 크기의 소재나 별도 승인된 변환/렌더 규격이 필요합니다. 큰 이미지 생성이 곧 카드뉴스 완성이라는 뜻은 아닙니다.

검토한 결과에 한해서 기존 `import-result`를 사용합니다. 입력 지문이 바뀌었다면 오래된 후보를 재사용하지 않습니다. 최종 manifest와 후보의 관계, 파일 경로, 정확한 권리/근거 플래그를 확인하세요. 생성 결과·증거·추적 링크·설정/키를 공개 저장소에 올리지 마세요.

## Python 통합과 예산

```python
from honeytem.providers import ProviderBudget, execute_provider, load_provider_config
from honeytem.schema import load_manifest

config = load_provider_config("work/provider.json")
job = load_manifest("work/input/manifest.json")
budget = ProviderBudget(max_requests=1, max_output_tokens=2048)
result = execute_provider(
    job, "01", "editorial_copy", config,
    allow_network=True, allow_remote=False, source_material=[], budget=budget,
)
# result.adapter_result, result.trace, result.image_bytes, result.image_filename
# 호출 자체는 파일 저장/검토/승인/게시를 하지 않습니다.
```

배치에서 동일 `ProviderBudget`을 순차 호출들에 재사용하면 요청 수와 최대 출력 토큰 예약량이 누적됩니다. 429/실패에서도 예약을 돌려주지 않습니다. 프로세스 재시작 뒤의 계정 전체 예산이나 과금 상한이 아니며, 이미지 호출은 요청 수로만 계산합니다. 서버 청구 대시보드와 지출 제한을 대체하지 않습니다. 동시 호출용 공유 카운터로 쓰지 마세요.

## 검증된 것과 검증되지 않은 것

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_providers.py' -v
```

테스트는 로컬 루프백 서버에서 실제 HTTP 요청/응답 계약, 네트워크 opt-in, 구조 검증, 리다이렉트/사설 DNS 차단, 재시도·요청/출력 예산, 타임아웃/느린 헤더·본문, 크기 제한, 오류/런타임 키 반사 방지, 승인 장벽, 이미지 원본 바이트 보존을 검증합니다. fixture 응답/그림을 모델의 성능이나 실제 생성 증거로 제시하지 않습니다.

원격 DNS는 반환된 모든 주소를 확인한 뒤 검사한 주소에 직접 연결합니다. 링크로컬·사설·metadata·multicast·예약 주소는 거부하고, 리다이렉트·환경 proxy를 사용하지 않으며 TLS 검증을 끄지 않습니다. 정상/중첩 JSON과 이미지 메타데이터에 반사된 현재 인증키는 거부하지만, 악의적인 서버의 모든 정보 은닉/픽셀 내 비밀 표현까지 탐지하는 DLP 장치는 아닙니다. 신뢰할 수 있는 제공자에 필요한 자료만 보내야 합니다.

실제 모델의 사실성·미학·속도·과금·라이선스, Mac 환경, 사용자 실상품 입력은 별도 검증 대상입니다. 이번 계약 테스트 통과는 이전 AI 작업 대비 품질/성능의 비회귀 증명이 아닙니다.

참고한 공식 계약: [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility), [OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat), [OpenAI Images API](https://developers.openai.com/api/reference/resources/images). 제공자는 부분 호환일 수 있으며 각 모델의 현재 지원 파라미터는 운영자가 확인해야 합니다. 코드는 특정 모델 이름이나 현재 상품 가격을 고정하지 않습니다.
