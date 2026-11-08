class Crawl2mdError(Exception):
    pass


class ValidationError(Crawl2mdError):
    pass


class NotVerifiedError(Crawl2mdError):
    pass


class FetchError(Crawl2mdError):
    pass


class Blocked(FetchError):
    pass


class ConvertError(Crawl2mdError):
    pass
