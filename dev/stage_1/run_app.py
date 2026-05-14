import multiprocessing
import sys

import uvicorn


if __name__ == "__main__":
    sys._base_executable = sys.executable
    multiprocessing.set_executable(sys.executable)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="error",
        reload=False,
        workers=1,
    )
