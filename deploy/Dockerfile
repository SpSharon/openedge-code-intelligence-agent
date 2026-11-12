# The OpenEdge agent service as a container image for AWS Lambda.
# Build from the repository root (see deploy/README.md):
#   docker build --platform linux/amd64 --provenance=false -f deploy/Dockerfile -t oe-agent:local .
#
# On Lambda, the AWS Lambda Web Adapter (a Lambda extension) turns each
# Function URL request into an HTTP request to uvicorn on port 8000, so the
# FastAPI app runs unchanged. Outside Lambda the extension does nothing and
# the same image runs with plain `docker run`.
FROM python:3.11-slim

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OE_INDEX_DIR=/app/index \
    AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_INVOKE_MODE=response_stream

WORKDIR /app

COPY requirements.txt requirements-core.txt
COPY service/requirements.txt requirements-service.txt
COPY deploy/requirements-lambda.txt requirements-lambda.txt
RUN pip install --no-cache-dir -r requirements-core.txt \
        -r requirements-service.txt -r requirements-lambda.txt

COPY openedge_agent ./openedge_agent
COPY corpus ./corpus
COPY service ./service

# The index is generated, not committed: build it into the image (numpy
# only, offline, deterministic). Lambda's file system is read-only, so
# nothing is written at run time; compile the bytecode now instead.
RUN python -m openedge_agent.ingest --root /app \
    && test -f /app/index/chunks.json \
    && python -m compileall -q openedge_agent service

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8000"]
