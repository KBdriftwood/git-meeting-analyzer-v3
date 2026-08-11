#!/bin/bash
cd /Users/kobayashiyuuki/projects/git-meeting-analyzer-v3
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
