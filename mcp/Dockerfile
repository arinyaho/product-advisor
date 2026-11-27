FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py advisor_graph.py concept_map.json book_library.py library_index.py library_cli.py ./
ARG BOOK_LIBRARY_DEPLOYMENT=""
RUN printf '%s\n' "$BOOK_LIBRARY_DEPLOYMENT" > /app/library-deployment
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
ENTRYPOINT ["python", "server.py"]
