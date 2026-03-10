up:
	docker compose up --build

up-isolation:
	docker compose -f docker-compose.yml -f docker-compose.isolation.yml up --build

up-reverse-ssh:
	docker compose --profile reverse-ssh up --build

test:
	docker compose run --no-deps --rm controller python -m unittest discover -s tests -v

smoke-isolation:
	./scripts/smoke_isolated_session.sh

smoke-isolation-tunnel:
	./scripts/smoke_isolated_session_tunnel.sh

smoke-reverse-ssh:
	./scripts/smoke_reverse_ssh.sh

bootstrap-codex-auth:
	./scripts/bootstrap_cli_auth.sh codex

bootstrap-claude-auth:
	./scripts/bootstrap_cli_auth.sh claude

bootstrap-gemini-auth:
	./scripts/bootstrap_cli_auth.sh gemini

bootstrap-all-auth:
	./scripts/bootstrap_cli_auth.sh all

down:
	docker compose down

config:
	docker compose config

config-isolation:
	docker compose -f docker-compose.yml -f docker-compose.isolation.yml config

config-reverse-ssh:
	docker compose --profile reverse-ssh config
