# Reference agent container for AgentCore Runtime (design §13.7).
# Framework-agnostic: a thin HTTP server (agent/server.py) honouring the AgentCore
# invocation protocol, driving the pure agate orchestration. Institutions may swap in
# their own framework behind the same protocol without touching the gateway.
#
# Python 3.13 (CLAUDE.md: 3.12/3.13 for every runtime; never 3.9).
FROM public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

# boto3 + pydantic + PyJWT (the last for agate.jwt_verify's real token verification).
RUN pip install --no-cache-dir "boto3>=1.34" "pydantic>=2.7" "pyjwt[crypto]>=2.8"

# The pure orchestration + the container shims. Copy only what the agent needs.
COPY agate/ ./agate/
COPY agent/ ./agent/

# AgentCore Runtime invokes over HTTP on 8080 (POST /invocations, GET /ping).
EXPOSE 8080
ENV PORT=8080

# Run as a non-root user (defence in depth; the microVM is already isolated).
RUN useradd --create-home --uid 10001 agate && chown -R agate /app
USER agate

CMD ["python", "-m", "agent.server"]
