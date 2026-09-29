"""Heavy work capacity is independent of Claude development terminals."""
import threading

DOCKER_WORK = threading.BoundedSemaphore(2)
