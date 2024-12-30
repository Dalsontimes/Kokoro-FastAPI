FROM nvidia/cuda:12.1.0-base-ubuntu22.04

ARG KOKORO_REPO
ARG KOKORO_COMMIT

# Install base system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-dev \
    espeak-ng \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install heavy Python dependencies first (better layer caching)
RUN pip3 install --no-cache-dir \
    phonemizer==3.3.0 \
    torch==2.5.1 \
    transformers==4.47.1 \
    scipy==1.14.1 \
    numpy==2.2.1 \
    munch==4.0.0

# Install API dependencies
RUN pip3 install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    pydantic==2.10.4 \
    pydantic-settings==2.7.0 \
    python-dotenv==1.0.1

# Set working directory
WORKDIR /app

# --(can skip if pre-cloning the repo)--
# Install git-lfs 
RUN apt-get update && apt-get install -y git-lfs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install

# Clone Kokoro repo at specific commit
RUN git clone ${KOKORO_REPO} . && \
    git checkout ${KOKORO_COMMIT}
# --------------------------------------
    
# Create output directory
RUN mkdir -p output

# Run with Python unbuffered output for live logging
ENV PYTHONUNBUFFERED=1

# Copy API files over
COPY api/src /app/api/src

# Set Python path
ENV PYTHONPATH=/app

# Run FastAPI server
CMD ["uvicorn", "api.src.main:app", "--host", "0.0.0.0", "--port", "8880"]
