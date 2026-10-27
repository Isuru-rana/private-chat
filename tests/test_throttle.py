from privatechat.throttle import Throttle


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_five_free():
    th = Throttle(clock=Clock())
    for _ in range(5):
        th.fail()
    assert th.retry_after() == 0


def test_backoff():
    c = Clock()
    th = Throttle(clock=c)
    for _ in range(5):
        th.fail()
    expected = [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    for e in expected:
        th.fail()
        assert th.retry_after() == e
        c.t += e  # wait it out before next attempt


def test_wait_then_allowed():
    c = Clock()
    th = Throttle(clock=c)
    for _ in range(6):
        th.fail()
    c.t += 1.5
    assert th.retry_after() == 0.5
    c.t += 0.5
    assert th.retry_after() == 0


def test_success_resets():
    th = Throttle(clock=Clock())
    for _ in range(7):
        th.fail()
    th.success()
    th.fail()
    assert th.retry_after() == 0


def test_many_failures_stay_capped():
    th = Throttle(clock=Clock())
    for _ in range(2000):
        th.fail()
    assert th.retry_after() == 60.0
