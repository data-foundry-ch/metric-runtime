# Optional container image for metric-runtime / PyPizza

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples
COPY metric-runtime.yaml ./

RUN pip install --no-cache-dir -e ".[demo]"

CMD ["metric-runtime", "--help"]
