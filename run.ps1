# 윈도우 실행 진입점. run.sh 와 같은 일을 한다.
#   .\run.ps1 --stage crawl        수집 → 1단계 지시서 (키 없이 됨)
#   .\run.ps1 --stage title        제목 확정 → 2단계 지시서
#   .\run.ps1 --stage finish       검증 → post.txt
#   .\run.ps1 --dry-run            API 경로, 제목까지만
#   .\run.ps1 --celeb 김희선
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Args)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# .env 가 있으면 읽어들인다 (키를 시스템 환경변수에 안 넣어도 되게)
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env" -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
        if ($v -ne "") { Set-Item -Path "Env:$k" -Value $v }
    }
}

if (-not $env:NAVER_CLIENT_ID -or -not $env:NAVER_CLIENT_SECRET) {
    Write-Output "[알림] 네이버 키가 없어 Google News RSS로 수집합니다."
}

# ANTHROPIC_API_KEY 는 API 경로에서만 필요하다. --stage 경로는 에이전트가 글을 쓴다.
if (($Args -notcontains "--stage") -and (-not $env:ANTHROPIC_API_KEY)) {
    Write-Output "[중단] ANTHROPIC_API_KEY 가 없습니다."
    Write-Output "       .env.example 을 .env 로 복사해 채우거나,"
    Write-Output "       키 없이 가려면 에이전트 경로를 쓰세요: .\run.ps1 --stage crawl"
    exit 2
}

New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$env:PYTHONIOENCODING = "utf-8"

python main.py @Args
exit $LASTEXITCODE
