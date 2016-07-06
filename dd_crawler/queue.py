from collections import Counter
from functools import lru_cache
import logging
import random
import time
from typing import Optional, List, Tuple, Union, Dict
from urllib.parse import urlsplit
from zlib import crc32

from deepdeep.utils import softmax
import numpy as np
from scrapy import Request
from scrapy_redis.queue import Base

from .utils import warn_if_slower


logger = logging.getLogger(__name__)


def cacheforawhile(method):
    """ Cache method for some time, so that it does not become a bottleneck.
    """
    max_cache_time = 180  # seconds
    cache_time_multiplier = 20
    last_call_time = None
    initial_cache_time = 0.5  # seconds
    cache_time = initial_cache_time

    @lru_cache(maxsize=1)
    def cached_method(*args, **kwargs):
        nonlocal cache_time
        kwargs.pop('time_key')
        t0 = time.time()
        try:
            return method(*args, **kwargs)
        finally:
            run_time = time.time() - t0
            cache_time = min(max_cache_time, run_time * cache_time_multiplier)
            if cache_time > initial_cache_time:
                logger.info('{} took {:.2f} s, new cache time is {:.1f} s'
                            .format(method.__name__, run_time, cache_time))

    def inner(self, *args, **kwargs):
        if self.skip_cache:
            return method(self, *args, **kwargs)
        nonlocal last_call_time
        t = time.time()
        if not last_call_time or (t - last_call_time > cache_time):
            last_call_time = t
        kwargs['time_key'] = last_call_time
        return cached_method(self, *args, **kwargs)

    return inner


# Note about race conditions: there are several workers executing this code, but
# - Redis itself is single-threaded
# - Only one worker should be crawling given domain, unless workers enter/leave


class BaseRequestQueue(Base):
    """ Request queue where each domain has a separate queue,
    and each domain is crawled only by one worker to be polite.

    QUEUE_CACHE_TIME setting determines the time queues are cached for,
    when workers do not change (stale cache only leads to missing new domains
    for a while, so it's safe to set it to higher values).
    """
    def __init__(self, *args, slots_mock=None, skip_cache=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.len_key = '{}:len'.format(self.key)  # redis int
        self.queues_key = '{}:queues'.format(self.key)  # redis sorted set
        self.workers_key = '{}:workers'.format(self.key)  # redis set
        self.worker_id_key = '{}:worker-id'.format(self.key)  # redis int
        self.worker_id = self.server.incr(self.worker_id_key)
        self.alive_timeout = 120  # seconds
        self.im_alive()
        self.n_requests = 0
        self.stat_each = 1000  # requests
        self.slots_mock = slots_mock
        self.skip_cache = skip_cache

    def __len__(self):
        return int(self.server.get(self.len_key) or '0')

    def push(self, request: Request):
        data = self._encode_request(request)
        score = -request.priority
        queue_key = self.request_queue_key(request)
        added = self.server.zadd(queue_key, **{data: score})
        if added:
            self.server.incr(self.len_key)
        top = self.server.zrange(queue_key, 0, 0, withscores=True)
        if top:
            (_, queue_score), = top
        else:  # a race during domain re-balancing: do not care about score much
            logger.warning('Placing a possibly incorrect queue score')
            queue_score = score
        queue_added = self.server.zadd(
            self.queues_key, **{queue_key: queue_score})
        if queue_added:
            logger.debug('ADD queue {}'.format(queue_key))

    def pop(self, timeout=0) -> Optional[Request]:
        self.n_requests += 1
        if self.n_requests % self.stat_each == 0:
            logger.info('Queue size: {}, domains: {}'.format(
                len(self), self.server.zcard(self.queues_key)))
        queue_key = self.select_queue_key()
        if queue_key:
            results = self.pop_from_queue(queue_key, 1)
            if results:
                return results[0]

    def clear(self):
        keys = {
            self.len_key, self.queues_key, self.workers_key, self.worker_id_key}
        keys.update(self.get_workers())
        keys.update(self.get_queues())
        self.server.delete(*keys)
        super().clear()

    def get_queues(self, withscores=False) \
            -> Union[List[bytes], List[Tuple[bytes, float]]]:
        return self.server.zrange(self.queues_key, 0, -1, withscores=withscores)

    def get_workers(self) -> List[bytes]:
        return self.server.smembers(self.workers_key)

    @warn_if_slower(0.1, logger)
    def select_queue_key(self) -> Optional[bytes]:
        """ Select which queue (domain) to use next.
        """
        idx, n_idx = self.discover()
        self.get_my_queues(idx, n_idx)  # This is a caching trick:
        # the trick above is needed because get_available_queues calls
        # get_my_queues, which is also cached, but we want independent
        # runtime estimates for them. So we cache get_my_queues here, and
        # runtime of get_available_queues does not include get_my_queues.
        # TODO - track this in cacheforawhile
        queue = self.select_best_queue(idx, n_idx)
        if queue:
            if self.server.zcard(queue):
                return queue
            else:
                self.remove_empty_queue(queue)

    def select_best_queue(self, idx: int, n_idx: int) -> Optional[bytes]:
        """ Select queue to crawl from, taking free slots into account.
        """
        available_queues, scores = self.get_available_queues(idx, n_idx)
        if available_queues:
            return random.choice(available_queues)

    @cacheforawhile
    def get_available_queues(self, idx, n_idx) -> \
            Tuple[List[bytes], Optional[np.ndarray]]:
        """ Return all queues with free slots (or just all) and their weights.
        """
        queues = self.get_my_queues(idx, n_idx)
        slots = self.get_slots()
        available_queues, scores = [], []
        all_queues, all_scores = [], []
        for q, s in queues.items():
            all_scores.append(s)
            all_queues.append(q)
            domain = self.queue_key_domain(q)
            if domain not in slots or slots[domain].free_transfer_slots():
                available_queues.append(q)
                scores.append(s)
        return ((available_queues, np.array(scores)) if available_queues else
                (all_queues, np.array(all_scores)))

    def get_slots(self) -> Dict:
        return (self.spider.crawler.engine.downloader.slots
                if self.slots_mock is None else self.slots_mock)

    def has_free_slots(self, queue: bytes, slots: Dict) -> bool:
        domain = self.queue_key_domain(queue)
        return domain not in slots or slots[domain].free_transfer_slots()

    @cacheforawhile
    def get_my_queues(self, idx: int, n_idx: int) -> Dict[bytes, float]:
        """ Get queues belonging to this worker.
        Here we cache not only expensive redis call, but a list comprehension
        below too.
        time_step key makes the cache live self.queue_cache_time seconds.
        Stale cache means we are not seeing new domains, nothing more.
        """
        queues = self.get_queues(withscores=True)
        return {q: s for q, s in queues if crc32(q) % n_idx == idx}

    def discover(self) -> Tuple[int, int]:
        """ Return a tuple of (my index, total number of workers).
        When workers connect or disconnect, this will cause re-distribution
        of domains between workers, but this is not an issue.
        """
        self.im_alive()
        worker_ids = set(map(int, self.get_workers()))
        for worker_id in list(worker_ids):
            if not self.is_alive(worker_id):
                self.server.srem(self.workers_key, worker_id)
                worker_ids.remove(worker_id)
        if self.worker_id in worker_ids:
            worker_ids = sorted(worker_ids)
            return worker_ids.index(self.worker_id), len(worker_ids)
        else:
            # This should not happen normally
            logger.warning('No live workers: selecting self!')
            return 0, 1

    def im_alive(self):
        """ Tell the server that current worker is alive.
        """
        pipe = self.server.pipeline()
        pipe.multi()
        pipe.sadd(self.workers_key, self.worker_id)\
            .set(self._worker_key(self.worker_id), 'ok', ex=self.alive_timeout)\
            .execute()

    def is_alive(self, worker_id) -> bool:
        """ Return whether given worker is alive.
        """
        return bool(self.server.get(self._worker_key(worker_id)))

    def _worker_key(self, worker_id) -> str:
        return '{}:worker-{}'.format(self.key, worker_id)

    def pop_from_queue(self, queue_key: bytes, n: int) -> List[Request]:
        """ Pop values with highest priorities from the given queue.
        """
        pipe = self.server.pipeline()
        pipe.multi()
        # Get one extra element to know new max score after pop
        pipe.zrange(queue_key, 0, n, withscores=True)\
            .zremrangebyrank(queue_key, 0, n - 1)
        results, count = pipe.execute()
        if results:
            self.server.decr(self.len_key, count)
            if len(results) == n + 1:
                _, queue_score = results[-1]
                self.server.zadd(
                    self.queues_key, **{queue_key.decode('utf8'): queue_score})
            else:
                self.remove_empty_queue(queue_key)
            return [self._decode_request(r) for r, _ in results[:n]]
        else:
            # queue was empty: remove it from queues set
            self.remove_empty_queue(queue_key)
            return []

    def remove_empty_queue(self, queue_key: bytes) -> None:
        # FIXME - maybe we should not remove empty queue keys? That can be racy
        removed = self.server.zrem(self.queues_key, queue_key)
        if removed:
            logger.debug('REM queue {}'.format(queue_key))

    def request_queue_key(self, request: Request) -> str:
        """ Key for request queue (based on it's domain).
        """
        domain = urlsplit(request.url).netloc
        return '{}:domain:{}'.format(self.key, domain)

    def queue_key_domain(self, queue_key: bytes) -> str:
        queue_key = queue_key.decode('utf8')
        prefix = '{}:domain:'.format(self.key)
        assert queue_key.startswith(prefix)
        return queue_key[len(prefix):]

    def get_stats(self):
        """ Return all queue stats.
        """
        queues = self.get_queues(withscores=True)
        return dict(
            len=len(self),
            n_domains=len(queues),
            queues=[
                (name.decode('utf8'), -score, self.server.zcard(name))
                for name, score in queues],
        )


class CompactQueue(BaseRequestQueue):
    """ A more compact request representation:
    preserve only url, depth and priority.
    """

    def _encode_request(self, request: Request) -> str:
        return '{} {} {}'.format(
            int(request.priority),
            request.meta.get('depth', 0),
            request.url)

    def _decode_request(self, encoded_request: bytes) -> Request:
        priority, depth, url = encoded_request.decode('utf-8').split(' ', 2)
        return Request(
            url, priority=int(priority), meta={'depth': int(depth)})


class SoftmaxQueue(CompactQueue):
    def select_best_queue(self, idx: int, n_idx: int) -> Optional[bytes]:
        """ Select queue taking weights into account.
        """
        available_queues, scores = self.get_available_queues(idx, n_idx)
        if available_queues:
            temprature = (
                self.spider.settings.getfloat('DD_BALANCING_TEMPERATURE') *
                self.spider.settings.getfloat('DD_PRIORITY_MULTIPLIER'))
            p = softmax(-scores, t=temprature)
            queue = np.random.choice(available_queues, p=p)
            slots = self.get_slots()
            if not self.has_free_slots(queue, slots):
                # It's possible to sample more than one queue above and check
                # which one has free slots. But for now log when we select a
                # "bad" queue - it seems to be extremely rare in practice.
                logger.info('Selected queue has no free slots')
            return queue


class BatchQueue(CompactQueue):
    batch_size = 100

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.local_queue = []

    def __len__(self):
        # FIXME - this is not quite correct, because super().__len__ is total
        # queue size, but self.local_queue is for this worker only.
        return super().__len__() + len(self.local_queue)

    def pop(self, timeout=0) -> Optional[Request]:
        self.local_queue = self.local_queue or self.pop_multi()
        if self.local_queue:
            return self.local_queue.pop()

    def pop_multi(self) -> List[Request]:
        idx, n_idx = self.discover()
        self.get_my_queues(idx, n_idx)  # This is a caching trick (see above)
        queues = self.select_best_queues(idx, n_idx)
        local_queue = []
        for queue, n in Counter(queues).items():
            local_queue.extend(reversed(self.pop_from_queue(queue, n)))
        return local_queue

    def select_best_queues(self, idx: int, n_idx: int) -> List[bytes]:
        available_queues, scores = self.get_available_queues(idx, n_idx)
        if available_queues:
            return list(np.random.choice(
                available_queues, size=self.batch_size))
        else:
            return []
