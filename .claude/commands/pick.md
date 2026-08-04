---
description: 인물을 지정해서 포스팅 생성 (예: /pick 전지현)
argument-hint: <연예인 이름>
---

`$ARGUMENTS` 로 오늘자 포스팅을 만들어줘. 진행 방식은 `/daily` 와 같다 —
API 키가 있으면 한 번에, 없으면 4단계 경로로 간다. 인물 지정만 붙는다.

```
.\run.ps1 --celeb "$ARGUMENTS"                 # API 경로
.\run.ps1 --stage crawl --celeb "$ARGUMENTS"   # 에이전트 경로 (1단계에만 붙이면 된다)
```

2단계부터는 `meta.json` 에 인물이 기록돼 있으니 `--celeb` 을 다시 붙일 필요가 없다.

해당 인물이 45일 쿨다운 중이면 경고가 뜨지만 그대로 진행된다.
쿨다운을 존중하고 싶으면 먼저 확인해라:

```
python -c "import store; store.init(); print(store.celeb_on_cooldown('$ARGUMENTS'))"
```

끝나면 확정 제목·점수와 남은 위반을 보고해줘.
