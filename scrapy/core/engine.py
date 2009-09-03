"""
This is the Scrapy engine which controls the Scheduler, Downloader and Spiders.

For more information see docs/topics/architecture.rst

"""
from datetime import datetime

from twisted.internet import reactor, task, defer
from twisted.python.failure import Failure
from scrapy.xlib.pydispatch import dispatcher

from scrapy import log
from scrapy.stats import stats
from scrapy.conf import settings
from scrapy.core import signals
from scrapy.core.downloader import Downloader
from scrapy.core.scraper import Scraper
from scrapy.core.exceptions import IgnoreRequest, DontCloseDomain
from scrapy.http import Response, Request
from scrapy.spider import spiders
from scrapy.utils.misc import load_object
from scrapy.utils.signal import send_catch_log
from scrapy.utils.defer import mustbe_deferred

class ExecutionEngine(object):

    def __init__(self):
        self.configured = False
        self.keep_alive = False
        self.closing = {} # dict (domain -> reason) of spiders being closed
        self.running = False
        self.killed = False
        self.paused = False
        self._next_request_pending = set()
        self._mainloop_task = task.LoopingCall(self._mainloop)

    def configure(self):
        """
        Configure execution engine with the given scheduling policy and downloader.
        """
        self.scheduler = load_object(settings['SCHEDULER'])()
        self.domain_scheduler = load_object(settings['DOMAIN_SCHEDULER'])()
        self.downloader = Downloader()
        self.scraper = Scraper(self)
        self.configured = True

    def start(self):
        """Start the execution engine"""
        if self.running:
            return
        self.start_time = datetime.utcnow()
        send_catch_log(signal=signals.engine_started, sender=self.__class__)
        self._mainloop_task.start(5.0, now=True)
        reactor.callWhenRunning(self._mainloop)
        self.running = True

    def stop(self):
        """Stop the execution engine gracefully"""
        if not self.running:
            return
        self.running = False
        for domain in self.open_domains:
            reactor.addSystemEventTrigger('before', 'shutdown', \
                self.close_domain, domain, reason='shutdown')
        if self._mainloop_task.running:
            self._mainloop_task.stop()

    def kill(self):
        """Forces shutdown without waiting for pending transfers to finish.
        stop() must have been called first
        """
        if self.running:
            return
        self.killed = True

    def pause(self):
        """Pause the execution engine"""
        self.paused = True

    def unpause(self):
        """Resume the execution engine"""
        self.paused = False

    def is_idle(self):
        return self.scheduler.is_idle() and self.downloader.is_idle() and \
            self.scraper.is_idle()

    def next_domain(self):
        domain = self.domain_scheduler.next_domain()
        if domain:
            self.open_domain(domain)
        return domain

    def next_request(self, domain, now=False):
        """Scrape the next request for the domain passed.

        The next request to be scraped is retrieved from the scheduler and
        requested from the downloader.

        The domain is closed if there are no more pages to scrape.
        """
        if now:
            self._next_request_pending.discard(domain)
        elif domain not in self._next_request_pending:
            self._next_request_pending.add(domain)
            return reactor.callLater(0, self.next_request, domain, now=True)
        else:
            return

        if self.paused:
            return reactor.callLater(5, self.next_request, domain)

        while not self._needs_backout(domain):
            if not self._next_request(domain):
                break

        if self.domain_is_idle(domain):
            self._domain_idle(domain)

    def _needs_backout(self, domain):
        return not self.running \
            or self.domain_is_closed(domain) \
            or self.downloader.sites[domain].needs_backout() \
            or self.scraper.sites[domain].needs_backout()

    def _next_request(self, domain):
        # Next pending request from scheduler
        request, deferred = self.scheduler.next_request(domain)
        if request:
            spider = spiders.fromdomain(domain)
            dwld = mustbe_deferred(self.download, request, spider)
            dwld.chainDeferred(deferred).addBoth(lambda _: deferred)
            dwld.addErrback(log.err, "Unhandled error on engine._next_request")
            return dwld

    def domain_is_idle(self, domain):
        scraper_idle = domain in self.scraper.sites \
            and self.scraper.sites[domain].is_idle()
        pending = self.scheduler.domain_has_pending_requests(domain)
        downloading = domain in self.downloader.sites \
            and self.downloader.sites[domain].active
        return scraper_idle and not (pending or downloading)

    def domain_is_closed(self, domain):
        """Return True if the domain is fully closed (ie. not even in the
        closing stage)"""
        return domain not in self.downloader.sites

    def domain_is_open(self, domain):
        """Return True if the domain is fully opened (ie. not in closing
        stage)"""
        return domain in self.downloader.sites and domain not in self.closing

    @property
    def open_domains(self):
        return self.downloader.sites.keys()

    def crawl(self, request, spider):
        schd = mustbe_deferred(self.schedule, request, spider)
        schd.addBoth(self.scraper.enqueue_scrape, request, spider)
        schd.addErrback(log.err, "Unhandled error on engine.crawl()")
        schd.addBoth(lambda _: self.next_request(spider.domain_name))

    def schedule(self, request, spider):
        domain = spider.domain_name
        if domain in self.closing:
            raise IgnoreRequest()
        if not self.scheduler.domain_is_open(domain):
            self.scheduler.open_domain(domain)
            if self.domain_is_closed(domain): # scheduler auto-open
                self.domain_scheduler.add_domain(domain)
        self.next_request(domain)
        return self.scheduler.enqueue_request(domain, request)

    def _mainloop(self):
        """Add more domains to be scraped if the downloader has the capacity.

        If there is nothing else scheduled then stop the execution engine.
        """
        if not self.running or self.paused:
            return

        while self.running and self.downloader.has_capacity():
            if not self.next_domain():
                return self._stop_if_idle()

    def download(self, request, spider):
        domain = spider.domain_name
        referer = request.headers.get('Referer')

        def _on_success(response):
            """handle the result of a page download"""
            assert isinstance(response, (Response, Request))
            if isinstance(response, Response):
                response.request = request # tie request to response received
                log.msg("Crawled %s (referer: <%s>)" % (response, referer), \
                    level=log.DEBUG, domain=domain)
                return response
            elif isinstance(response, Request):
                newrequest = response
                schd = mustbe_deferred(self.schedule, newrequest, spider)
                schd.chainDeferred(newrequest.deferred)
                return newrequest.deferred

        def _on_error(_failure):
            """handle an error processing a page"""
            exc = _failure.value
            if isinstance(exc, IgnoreRequest):
                errmsg = _failure.getErrorMessage()
                level = exc.level
            else:
                errmsg = str(_failure)
                level = log.ERROR
            if errmsg:
                log.msg("Downloading <%s> (referer: <%s>): %s" % (request.url, \
                    referer, errmsg), level=level, domain=domain)
            return Failure(IgnoreRequest(str(exc)))

        def _on_complete(_):
            self.next_request(domain)
            return _

        dwld = mustbe_deferred(self.downloader.fetch, request, spider)
        dwld.addCallbacks(_on_success, _on_error)
        dwld.addBoth(_on_complete)
        return dwld

    def open_domain(self, domain):
        log.msg("Domain opened", domain=domain)
        spider = spiders.fromdomain(domain)
        self.next_request(domain)

        self.downloader.open_domain(domain)
        self.scraper.open_domain(domain)
        stats.open_domain(domain)

        # XXX: sent for backwards compatibility (will be removed in Scrapy 0.8)
        send_catch_log(signals.domain_open, sender=self.__class__, \
            domain=domain, spider=spider)

        send_catch_log(signals.domain_opened, sender=self.__class__, \
            domain=domain, spider=spider)

    def _domain_idle(self, domain):
        """Called when a domain gets idle. This function is called when there
        are no remaining pages to download or schedule. It can be called
        multiple times. If some extension raises a DontCloseDomain exception
        (in the domain_idle signal handler) the domain is not closed until the
        next loop and this function is guaranteed to be called (at least) once
        again for this domain.
        """
        spider = spiders.fromdomain(domain)
        try:
            dispatcher.send(signal=signals.domain_idle, sender=self.__class__, \
                domain=domain, spider=spider)
        except DontCloseDomain:
            self.next_request(domain)
            return
        except:
            log.err("Exception catched on domain_idle signal dispatch")
        if self.domain_is_idle(domain):
            self.close_domain(domain, reason='finished')

    def _stop_if_idle(self):
        """Call the stop method if the system has no outstanding tasks. """
        if self.is_idle() and not self.keep_alive:
            self.stop()

    def close_domain(self, domain, reason='cancelled'):
        """Close (cancel) domain and clear all its outstanding requests"""
        if domain not in self.closing:
            log.msg("Closing domain (%s)" % reason, domain=domain)
            self.closing[domain] = reason
            self.downloader.close_domain(domain)
            self.scheduler.clear_pending_requests(domain)
            return self._finish_closing_domain_if_idle(domain)
        return defer.succeed(None)

    def _finish_closing_domain_if_idle(self, domain):
        """Call _finish_closing_domain if domain is idle"""
        if self.domain_is_idle(domain) or self.killed:
            self._finish_closing_domain(domain)
        else:
            dfd = defer.Deferred()
            dfd.addCallback(self._finish_closing_domain_if_idle)
            delay = 5 if self.running else 1
            reactor.callLater(delay, dfd.callback, domain)
            return dfd

    def _finish_closing_domain(self, domain):
        """This function is called after the domain has been closed"""
        spider = spiders.fromdomain(domain) 
        self.scheduler.close_domain(domain)
        self.scraper.close_domain(domain)
        reason = self.closing.pop(domain, 'finished')
        send_catch_log(signal=signals.domain_closed, sender=self.__class__, \
            domain=domain, spider=spider, reason=reason)
        stats.close_domain(domain, reason=reason)
        log.msg("Domain closed (%s)" % reason, domain=domain) 
        spiders.close_domain(domain)
        if self.running:
            self._mainloop()
        elif not self.open_domains:
            send_catch_log(signal=signals.engine_stopped, sender=self.__class__)

scrapyengine = ExecutionEngine()
