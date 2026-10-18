FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
# Three OpenCV wheels share the cv2 package (see pyproject.toml). Reinstall the
# pinned headless one last so its binary is the one that runs: the GUI builds
# need X11/GL libraries this slim image doesn't have.
RUN pip install --no-cache-dir --no-deps --force-reinstall "$(python -c "import tomllib; \
    deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; \
    print(next(d for d in deps if d.startswith('opencv-python-headless==')))")" \
 && python -c "import cv2; print('cv2', cv2.__version__)"
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
