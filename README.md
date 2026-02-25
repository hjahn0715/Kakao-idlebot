# IdleBot (Kakao Chatbot RPG)

카카오톡 챗봇 기반 RPG 게임 봇입니다.  
FastAPI + SQLite + Kakao OpenBuilder Webhook 구조로 구현했습니다.

## Features
- 유저 자동 생성 (카카오 user id 기반)
- 전투 시스템 (난이도 선택 버튼)
- 강화 시스템 (확률 기반)
- 유저 상태 DB 저장 (SQLite)
- 멀티턴 대화 처리 (pending state)

## Tech Stack
- Python (FastAPI)
- SQLite
- Kakao OpenBuilder
- ngrok (local webhook test)

## Commands
- 전투
- /내정보
- /강화
- /도움

## Architecture
Kakao Chatbot → OpenBuilder → FastAPI Webhook → SQLite