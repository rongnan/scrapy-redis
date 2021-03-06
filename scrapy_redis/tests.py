import os
import redis

from scrapy.http import Request
from scrapy.spider import BaseSpider
from unittest import TestCase

from .dupefilter import RFPDupeFilter
from .queue import SpiderQueue, SpiderPriorityQueue, SpiderStack
from .scheduler import Scheduler


# allow test settings from environment
REDIS_HOST = os.environ.get('REDIST_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))


class DupeFilterTest(TestCase):

    def setUp(self):
        self.server = redis.Redis(REDIS_HOST, REDIS_PORT)
        self.key = 'scrapy_redis:tests:dupefilter:'
        self.df = RFPDupeFilter(self.server, self.key)

    def tearDown(self):
        self.server.delete(self.key)

    def test_dupe_filter(self):
        req = Request('http://example.com')

        self.assertFalse(self.df.request_seen(req))
        self.assertTrue(self.df.request_seen(req))

        self.df.close('nothing')


class QueueTestMixin(object):

    queue_cls = None

    def setUp(self):
        self.spider = BaseSpider('myspider')
        self.key = 'scrapy_redis:tests:%s:queue' % self.spider.name
        self.server = redis.Redis(REDIS_HOST, REDIS_PORT)
        self.q = self.queue_cls(self.server, BaseSpider('myspider'), self.key)

    def tearDown(self):
        self.server.delete(self.key)

    def test_clear(self):
        self.assertEqual(len(self.q), 0)

        for i in range(10):
            # XXX: can't use same url for all requests as SpiderPriorityQueue
            # uses redis' set implemention and we will end with only one
            # request in the set and thus failing the test. It should be noted
            # that when using SpiderPriorityQueue it acts as a request
            # duplication filter whenever the serielized requests are the same.
            # This might be unwanted on repetitive requests to the same page
            # even with dont_filter=True flag.
            req = Request('http://example.com/?page=%s' % i)
            self.q.push(req)
        self.assertEqual(len(self.q), 10)

        self.q.clear()
        self.assertEqual(len(self.q), 0)


class SpiderQueueTest(QueueTestMixin, TestCase):

    queue_cls = SpiderQueue

    def test_queue(self):
        req1 = Request('http://example.com/page1')
        req2 = Request('http://example.com/page2')

        self.q.push(req1)
        self.q.push(req2)

        out1 = self.q.pop()
        out2 = self.q.pop()

        self.assertEqual(out1.url, req1.url)
        self.assertEqual(out2.url, req2.url)


class SpiderPriorityQueueTest(QueueTestMixin, TestCase):

    queue_cls = SpiderPriorityQueue

    def test_queue(self):
        req1 = Request('http://example.com/page1', priority=100)
        req2 = Request('http://example.com/page2', priority=50)
        req3 = Request('http://example.com/page2', priority=200)

        self.q.push(req1)
        self.q.push(req2)
        self.q.push(req3)

        out1 = self.q.pop()
        out2 = self.q.pop()
        out3 = self.q.pop()

        self.assertEqual(out1.url, req3.url)
        self.assertEqual(out2.url, req1.url)
        self.assertEqual(out3.url, req2.url)


class SpiderStackTest(QueueTestMixin, TestCase):

    queue_cls = SpiderStack

    def test_queue(self):
        req1 = Request('http://example.com/page1')
        req2 = Request('http://example.com/page2')

        self.q.push(req1)
        self.q.push(req2)

        out1 = self.q.pop()
        out2 = self.q.pop()

        self.assertEqual(out1.url, req2.url)
        self.assertEqual(out2.url, req1.url)


class SchedulerTest(TestCase):

    def setUp(self):
        self.server = redis.Redis(REDIS_HOST, REDIS_PORT)
        self.key_prefix = 'scrapy_redis:tests:'
        self.queue_key = self.key_prefix + '%(spider)s:requests'
        self.dupefilter_key = self.key_prefix + '%(spider)s:dupefilter'
        self.scheduler = Scheduler(self.server, False, self.queue_key,
                                   SpiderQueue, self.dupefilter_key)

    def tearDown(self):
        for key in self.server.keys(self.key_prefix):
            self.server.delete(key)

    def test_scheduler(self):
        # default no persist
        self.assertFalse(self.scheduler.persist)

        spider = BaseSpider('myspider')
        self.scheduler.open(spider)
        self.assertEqual(len(self.scheduler), 0)

        req = Request('http://example.com')
        self.scheduler.enqueue_request(req)
        self.assertTrue(self.scheduler.has_pending_requests())
        self.assertEqual(len(self.scheduler), 1)

        # dupefilter in action
        self.scheduler.enqueue_request(req)
        self.assertEqual(len(self.scheduler), 1)

        out = self.scheduler.next_request()
        self.assertEqual(out.url, req.url)

        self.assertFalse(self.scheduler.has_pending_requests())
        self.assertEqual(len(self.scheduler), 0)

        self.scheduler.close('finish')

    def test_scheduler_persistent(self):
        messages = []
        spider = BaseSpider('myspider')
        spider.log = lambda *args, **kwargs: messages.append([args, kwargs])

        self.scheduler.persist = True
        self.scheduler.open(spider)

        self.assertEqual(messages, [])

        self.scheduler.enqueue_request(Request('http://example.com/page1'))
        self.scheduler.enqueue_request(Request('http://example.com/page2'))

        self.assertTrue(self.scheduler.has_pending_requests())
        self.scheduler.close('finish')

        self.scheduler.open(spider)
        self.assertEqual(messages, [
            [('Resuming crawl (2 requests scheduled)',), {}],
        ])
        self.assertEqual(len(self.scheduler), 2)

        self.scheduler.persist = False
        self.scheduler.close('finish')

        self.assertEqual(len(self.scheduler), 0)
