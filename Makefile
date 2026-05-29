.PHONY: start stop kill restart logs

start:
	@echo "Starting MT5 Bridge..."
	@nohup .venv/bin/uvicorn user_data.mt5_bridge.web_server:app \
		--host 0.0.0.0 --port 8080 --log-level warning \
		> /tmp/bridge.log 2>&1 & echo "Server PID: $$!"
	@sleep 4
	@curl -s -X POST http://localhost:8080/api/bridge/start > /dev/null
	@echo "Bridge running at http://localhost:8080"

stop:
	@echo "Stopping MT5 Bridge..."
	@curl -s -X POST http://localhost:8080/api/bridge/stop > /dev/null 2>&1 || true
	@fuser -k 8080/tcp 2>/dev/null || true
	@echo "Bridge stopped."

kill:
	@fuser -k 8080/tcp 2>/dev/null && echo "Port 8080 killed." || echo "Nothing on port 8080."

restart: stop
	@sleep 2
	@$(MAKE) start

logs:
	@tail -f /tmp/bridge.log
