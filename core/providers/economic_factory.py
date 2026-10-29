from core.domain.economics import EconomicsMode
from core.providers.economic_adp import ADPReleasesProvider
from core.providers.economic_base import EconomicProvider
from core.providers.economic_bea_data import BEADataProvider
from core.providers.economic_bea_releases import BEAReleasesProvider
from core.providers.economic_bls import BLSEconomicProvider
from core.providers.economic_calendars import BEACalendarProvider, FedCalendarProvider
from core.providers.economic_census_releases import CensusReleasesProvider
from core.providers.economic_census_retail import CensusRetailProvider
from core.providers.economic_conference_board import ConferenceBoardProvider
from core.providers.economic_consensus import ForexFactoryProvider
from core.providers.economic_demo import DemoEconomicProvider
from core.providers.economic_dol_claims import DOLClaimsProvider
from core.providers.economic_fed_g17 import FedG17Provider
from core.providers.economic_fred import FREDEconomicProvider
from core.providers.economic_fred_calendar import FREDReleaseCalendarProvider
from core.providers.economic_http import Clock, utc_now
from core.providers.economic_ism_newswire import ISMNewswireProvider
from core.providers.economic_nar import NARReleasesProvider
from core.providers.economic_nyfed import NYFedEconomicProvider
from core.providers.economic_releases import FedReleasesProvider


def build_economic_providers(
    mode: EconomicsMode | str,
    *,
    fred_api_key: str | None = None,
    bls_api_key: str | None = None,
    census_api_key: str | None = None,
    bea_api_key: str | None = None,
    clock: Clock = utc_now,
) -> list[EconomicProvider]:
    selected = EconomicsMode(mode)
    if selected == EconomicsMode.DISABLED:
        return []
    if selected == EconomicsMode.DEMO:
        return [DemoEconomicProvider(clock=clock)]
    return [
        BLSEconomicProvider(api_key=bls_api_key, clock=clock),
        NYFedEconomicProvider(clock=clock),
        BEACalendarProvider(clock=clock),
        BEAReleasesProvider(clock=clock),
        BEADataProvider(api_key=bea_api_key, clock=clock),
        FedCalendarProvider(clock=clock),
        FedReleasesProvider(clock=clock),
        FREDReleaseCalendarProvider(clock=clock),
        FREDEconomicProvider(api_key=fred_api_key, clock=clock),
        ForexFactoryProvider(clock=clock),
        DOLClaimsProvider(clock=clock),
        ISMNewswireProvider(clock=clock),
        ADPReleasesProvider(clock=clock),
        NARReleasesProvider(clock=clock),
        CensusRetailProvider(api_key=census_api_key, clock=clock),
        CensusReleasesProvider(api_key=census_api_key, clock=clock),
        FedG17Provider(clock=clock),
        ConferenceBoardProvider(clock=clock),
    ]
