# Test Data Seeder for Distributed AI Runtime
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy seeder script and test data
COPY docker/test-seeder/seed_data.py .
COPY tests/data/ ./test_data/

# Create non-root user
RUN useradd --create-home --shell /bin/bash seeder
RUN chown -R seeder:seeder /app
USER seeder

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the seeder
CMD ["python", "seed_data.py"]
