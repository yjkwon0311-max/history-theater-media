# history-theater-media

역사 실화극장 채널의 미디어 호스팅 + 클라우드 발행 저장소.
- reels/ : 발행용 세로 쇼츠 mp4 (GitHub Pages로 공개 URL 제공)
- thumbs/ : 유튜브 썸네일
- posts.json : 발행 큐 (at=KST 시각)
- publish.py + .github/workflows/publish.yml : GitHub Actions 자동 발행 (유튜브+인스타)

토큰/시크릿은 커밋하지 않는다. 전부 GitHub Secrets(YT_CLIENT_SECRET_JSON, YT_TOKEN_JSON, IG_ACCESS_TOKEN).
