"""Bridge a model worker's consent request to the task polling API."""

from threading import Condition

from backend.env.model_switch import ModelSwitchSession


class RunModelApproval:
    def __init__(self, store, task_id):
        self.store, self.task_id = store, task_id
        self.condition = Condition()
        self.pending = None
        self.answer = None
        self.session = ModelSwitchSession(self.request)

    def request(self, request, cancelled):
        with self.condition:
            self.pending, self.answer = request, None
            self.store.set_model_approval(self.task_id, request.as_dict())
            try:
                while self.answer is None:
                    self.session.check(cancelled)
                    self.condition.wait(timeout=0.1)
                return self.answer
            finally:
                self.pending = None
                self.store.set_model_approval(self.task_id, None)

    def decide(self, request_id, allowed):
        with self.condition:
            if self.pending is None or self.pending.request_id != request_id or self.answer is not None:
                raise ValueError("模型切换请求已失效或已处理。")
            self.answer = allowed
            if not allowed:
                self.session.stop()
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.session.stop()
            self.condition.notify_all()
            self.store.set_model_approval(self.task_id, None)
