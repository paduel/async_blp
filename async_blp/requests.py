"""
Thus module contains wrappers for different types of Bloomberg requests
"""
import asyncio
import datetime as dt
import uuid
from collections import defaultdict
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import pandas as pd

from async_blp.base_request import RequestBase
from async_blp.errors import BloombergErrors
from async_blp.parser import parse_errors
from async_blp.parser import parse_field_data
from async_blp.parser import parse_reference_security_data
from .enums import ErrorBehaviour
from .enums import SecurityIdType
from .utils import log
from .utils.blp_name import SECURITY_DATA

# pylint: disable=ungrouped-imports
try:
    import blpapi
except ImportError:
    from async_blp.utils import env_test as blpapi

BloombergValue = Union[str, int, float, dt.date, dt.datetime,
                       Dict[str, Union[str, int, float, dt.date, dt.datetime]]]

LOGGER = log.get_logger()


class ReferenceDataRequest(RequestBase):
    """
    Convenience wrapper around Bloomberg's ReferenceDataRequest
    """
    service_name = "//blp/refdata"
    request_name = "ReferenceDataRequest"

    # pylint: disable=too-many-arguments
    def __init__(self,
                 securities: List[str],
                 fields: List[str],
                 security_id_type: Optional[SecurityIdType] = None,
                 overrides: Optional[Dict] = None,
                 error_behavior: ErrorBehaviour = ErrorBehaviour.RETURN,
                 loop: asyncio.AbstractEventLoop = None):

        if security_id_type is not None:
            securities = [security_id_type.add_type(security)
                          for security in securities]

        request_options = {
            'securities': securities,
            'fields':     fields,
            }

        if overrides:
            request_options.update(overrides)

        super().__init__(request_options, error_behavior, loop)

        self._securities = securities
        self._fields = fields
        self._overrides = overrides or {}
        self._security_id_type = security_id_type

    async def process(self) -> Tuple[pd.DataFrame, BloombergErrors]:
        """
        Asynchronously process events from `msg_queue` until the event with
        event type RESPONSE is received. This method doesn't check if received
        events belongs to this request and will return everything that
        can be parsed.

        Return format is pd.DataFrame with columns as fields and indexes
        as security_ids.
        """
        data_frame = pd.DataFrame(columns=self._fields,
                                  index=self._securities)
        errors = BloombergErrors()

        while True:

            msg: blpapi.Message = await self._get_message_from_queue()

            if msg is None:
                break

            security_data_element = msg.getElement(SECURITY_DATA)

            if security_data_element.isArray():
                msg_data = list(security_data_element.values())
            else:
                msg_data = [security_data_element]

            for security_data in msg_data:
                msg_frame = parse_reference_security_data(security_data)
                index = msg_frame.index
                columns = msg_frame.columns

                data_frame.loc[index, columns] = msg_frame

                security_errors = parse_errors(security_data,
                                               self._error_behaviour)
                if security_errors is not None:
                    errors += security_errors

        return data_frame, errors

    @property
    def weight(self):
        """
        Approximate number of returned values; used to balance load
        between handlers
        """
        return len(self._securities) * len(self._fields)


class HistoricalDataRequest(RequestBase):
    service_name = "//blp/refdata"
    request_name = 'HistoricalDataRequest'

    # pylint: disable=too-many-arguments
    def __init__(self,
                 securities: List[str],
                 fields: List[str],
                 start_date: dt.date,
                 end_date: dt.date,
                 security_id_type: Optional[SecurityIdType] = None,
                 overrides: Optional[Dict] = None,
                 error_behavior: ErrorBehaviour = ErrorBehaviour.RETURN,
                 loop: asyncio.AbstractEventLoop = None,
                 ):

        if security_id_type is not None:
            securities = [security_id_type.add_type(security)
                          for security in securities]

        request_options = {
            'securities': securities,
            'fields':     fields,
            'startDate':  start_date.strftime('%Y%m%d'),
            'endDate':    end_date.strftime('%Y%m%d'),
            }

        if overrides:
            request_options.update(overrides)

        super().__init__(request_options, error_behavior, loop)

        self._start_date = start_date
        self._end_date = end_date
        self._securities = securities
        self._fields = fields

    @property
    def weight(self):
        num_days = (self._end_date - self._start_date).days
        return len(self._fields) * len(self._securities) * num_days

    async def process(self) -> Tuple[pd.DataFrame, BloombergErrors]:
        """
        Asynchronously process events from `msg_queue` until the event with
        event type RESPONSE is received. This method doesn't check if received
        events belongs to this request and will return everything that
        can be parsed.

        Return format is pd.DataFrame with columns as fields and indexes
        as security_ids.
        """
        all_dates = pd.date_range(self._start_date, self._end_date)
        index = pd.MultiIndex.from_product([all_dates, self._securities],
                                           names=['date', 'security'])

        data_frame = pd.DataFrame(index=index,
                                  columns=self._fields)
        errors = BloombergErrors()

        while True:

            msg: blpapi.Message = await self._get_message_from_queue()

            if msg is None:
                break

            security_data_element = msg.getElement(SECURITY_DATA)

            msg_frame = parse_reference_security_data(security_data_element)
            index = msg_frame.index
            columns = msg_frame.columns

            data_frame.loc[index, columns] = msg_frame

            security_errors = parse_errors(security_data_element,
                                           self._error_behaviour)
            if security_errors is not None:
                errors += security_errors

        return data_frame, errors


class Subscription(ReferenceDataRequest):
    service_name = '//blp/mktdata'

    def __init__(self,
                 securities: List[str],
                 fields: List[str],
                 security_id_type: Optional[SecurityIdType] = None,
                 overrides: Optional[Dict] = None,
                 error_behavior: ErrorBehaviour = ErrorBehaviour.RETURN,
                 loop: asyncio.AbstractEventLoop = None,
                 ):
        super().__init__(securities,
                         fields,
                         security_id_type,
                         overrides,
                         error_behavior,
                         loop)
        self._security_mapping = {blpapi.CorrelationId(uuid.uuid4()):
                                      sec for sec in self._securities}

    def create_subscription(self) -> blpapi.SubscriptionList:
        subscription = blpapi.SubscriptionList()
        for cor_id, security in self._security_mapping.items():
            subscription.add(security,
                             self._fields,
                             correlationId=cor_id)

        return subscription

    def create(self, service: blpapi.Service) -> blpapi.Request:
        raise RuntimeError('Please use `create_subscription`')

    async def process(self) -> pd.DataFrame:
        """
        Asynchronously process events from `msg_queue` until the event will
        ended

        Return format is pd.DataFrame with columns as fields and indexes
        as security_ids.
        """
        data = defaultdict(dict)

        while not self._msg_queue.empty():
            LOGGER.debug('%s: waiting for messages', self.__class__.__name__)
            msg: blpapi.Message = self._msg_queue.get_nowait()

            for cor_id in msg.correlationIds():
                if cor_id not in self._security_mapping:
                    continue

                security_data_element = msg.asElement()
                for field in security_data_element.elements():
                    try:
                        field_name, field_value = parse_field_data(field)
                        isin = self._security_mapping[cor_id]
                        data[field_name][isin] = field_value

                    except blpapi.exception.IndexOutOfRangeException as ex:
                        # todo check what is happening (field is empty)
                        LOGGER.error(ex)

        return pd.DataFrame(data)


class SearchField(RequestBase):
    """
    FLDS lookup
    """

    service_name = "//blp/apiflds"
    request_name = "CategorizedFieldSearchRequest"

    def __init__(self,
                 query: str,
                 overrides: Optional[Dict] = None,
                 error_behavior: ErrorBehaviour = ErrorBehaviour.RETURN,
                 loop: asyncio.AbstractEventLoop = None,
                 ):

        request_options = {
            'searchSpec': query,
            }

        request_options.update(overrides)

        super().__init__(request_options,
                         error_behavior=error_behavior,
                         loop=loop)

        self._query = query

    async def process(self) -> pd.DataFrame:
        """
        Asynchronously process events from `msg_queue` until the event with
        event type RESPONSE is received. This method doesn't check if received
        events belongs to this request and will return everything that
        can be parsed.

        Return format is pd.DataFrame with columns as fields and indexes
        as security_ids.

        categorizedFieldResponse = {
            category[] = {
                category = {
                    categoryName = "Analysis"
                    categoryId = "4670040a019000e0"
                    numFields = 293
                    description = "Analysis"
                    isLeafNode = false
                    fieldData[] = {
                        fieldData = {
                            id = "OP179"
                            fieldInfo = {
                                mnemonic = "THETA_LAST"
                                description = "Theta Last Price"
                                datatype = Double
                                categoryName[] = {
                                }
                                property[] = {
                                }


        """
        data = defaultdict(dict)

        while True:

            msg = await self._get_message_from_queue()

            if msg is None:
                break

            for category_data in msg.getElement('category').values():
                # category[] = { ... }
                field_data_element = category_data.getElement('fieldData')

                for field in field_data_element.values():
                    # fieldData[] = { ... }
                    id_element = field.getElement('id')
                    _, id_value = parse_field_data(id_element)

                    for desc in field.getElement('fieldInfo').elements():
                        # fieldInfo ={ ... }
                        if desc.isArray():
                            # categoryName[] = { empty }
                            continue

                        name, value = parse_field_data(desc)
                        # description = "Theta Last Price"
                        data[name][id_value] = value

        return pd.DataFrame(data)

    @property
    def weight(self) -> int:
        return 1
