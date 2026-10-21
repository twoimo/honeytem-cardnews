<div align="center">

# 🍯 꿀템 카드뉴스

**제휴 링크 하나로, 인스타그램 꿀템 카드뉴스를 한 번에.**

쿠팡파트너스 · 카카오톡 선물하기 큐레이터 링크를 받아<br/>
카드뉴스 이미지, 캡션, 링크 목록을 만들어 주는 Grok Bot 설정입니다.

[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Grok%20Bot-6b5bd2.svg)
![Lang](https://img.shields.io/badge/lang-한국어-lightgrey.svg)

</div>

---

## ✨ 무엇을 하나요

| 입력 | 출력 |
| --- | --- |
| 쿠팡파트너스 링크 | 🖼️ 카드뉴스 이미지 |
| 카카오톡 선물하기 큐레이터 링크 | ✍️ 인스타그램 캡션 (대가성 문구 포함) |
| (선택) 주제, 예: "가을 집들이 선물" | 🔗 링크 목록 |

> 제휴 링크로 쇼핑·선물 추천 계정을 운영하는 분께 맞춰져 있어요.

## 🚀 빠른 시작

1. 봇에게 상품 링크를 붙여 넣습니다.
2. 주제가 있으면 한 줄로 알려 줍니다.
3. 이미지 · 캡션 · 링크 목록을 받아 바로 게시합니다.

## 🎨 디자인 원칙

- **흰 배경 매거진형**: 작은 마스트헤드 + 페이지 번호, 배경 지운 제품 사진, 굵은 한 줄 제목과 가격, 1~2줄 코멘트
- **10가지 변형**을 게시물마다 돌려 쓰기
- **미니멀 + 힙함**: 개성 있는 선, 독특한 타이포, 스티커·태그
- 배경은 흰색 · 오프화이트 · 연회색 · 검정만
- 🚫 형광펜 하이라이트, 원색 배경 금지

자세한 내용은 [`rules/design.md`](rules/design.md)를 보세요.

## 📁 구조

```
.
├── bot/
│   └── profile.json            # 봇 이름, 설명, 아바타
├── rules/
│   ├── design.md               # 카드뉴스 디자인 규칙
│   └── kakao-gift-curator.md   # 선물하기 큐레이터 링크·대가성 문구
├── LICENSE
└── README.md
```

## ⚖️ 대가성 표기

제휴 링크가 들어간 게시물에는 대가성 문구를 꼭 넣어 주세요. 카카오톡 선물하기 큐레이터 공식 문구는 [`rules/kakao-gift-curator.md`](rules/kakao-gift-curator.md)에 있어요.

## 📄 라이선스

[MIT](LICENSE)
