# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for pycairo and PDF generation
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set environment variables
ENV PORT=5000
ENV HOST=0.0.0.0

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define the command to run the application
CMD ["python3", "server.py"]
