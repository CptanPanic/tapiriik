from tapiriik.settings import WEB_ROOT
from tapiriik.services.service_base import ServiceAuthenticationType, ServiceBase
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit
from tapiriik.services.api import APIException, APIWarning, APIExcludeActivity, UserException, UserExceptionType
from tapiriik.services.tcx import TCXIO
from tapiriik.services.fit import FITIO
from tapiriik.services.sessioncache import SessionCache

from django.core.urlresolvers import reverse
import pytz
from datetime import datetime, timedelta
import requests
import os
import math
import logging
logger = logging.getLogger(__name__)

class GarminConnectService(ServiceBase):
    ID = "garminconnect"
    DisplayName = "Garmin Connect"
    AuthenticationType = ServiceAuthenticationType.UsernamePassword
    RequiresExtendedAuthorizationDetails = True

    _activityMappings = {
                                "running": ActivityType.Running,
                                "cycling": ActivityType.Cycling,
                                "mountain_biking": ActivityType.MountainBiking,
                                "walking": ActivityType.Walking,
                                "hiking": ActivityType.Hiking,
                                "resort_skiing_snowboarding": ActivityType.DownhillSkiing,
                                "cross_country_skiing": ActivityType.CrossCountrySkiing,
                                "backcountry_skiing_snowboarding": ActivityType.CrossCountrySkiing,  # ish
                                "skating": ActivityType.Skating,
                                "swimming": ActivityType.Swimming,
                                "rowing": ActivityType.Rowing,
                                "elliptical": ActivityType.Elliptical,
                                "all": ActivityType.Other  # everything will eventually resolve to this
    }

    _reverseActivityMappings = {  # Removes ambiguities when mapping back to their activity types
                                "running": ActivityType.Running,
                                "cycling": ActivityType.Cycling,
                                "mountain_biking": ActivityType.MountainBiking,
                                "walking": ActivityType.Walking,
                                "hiking": ActivityType.Hiking,
                                "resort_skiing_snowboarding": ActivityType.DownhillSkiing,
                                "cross_country_skiing": ActivityType.CrossCountrySkiing,
                                "skating": ActivityType.Skating,
                                "swimming": ActivityType.Swimming,
                                "rowing": ActivityType.Rowing,
                                "elliptical": ActivityType.Elliptical,
                                "other": ActivityType.Other  # I guess? (vs. "all" that is)
    }

    SupportedActivities = list(_activityMappings.values())

    SupportsHR = SupportsCadence = True

    _sessionCache = SessionCache(lifetime=timedelta(minutes=30), freshen_on_get=True)

    _unitMap = {
        "mph": ActivityStatisticUnit.MilesPerHour,
        "kph": ActivityStatisticUnit.KilometersPerHour,
        "hmph": ActivityStatisticUnit.HectometersPerHour,
        "hydph": ActivityStatisticUnit.HundredYardsPerHour,
        "celcius": ActivityStatisticUnit.DegreesCelcius,
        "fahrenheit": ActivityStatisticUnit.DegreesFahrenheit,
        "mile": ActivityStatisticUnit.Miles,
        "kilometer": ActivityStatisticUnit.Kilometers,
        "foot": ActivityStatisticUnit.Feet,
        "meter": ActivityStatisticUnit.Meters,
        "yard": ActivityStatisticUnit.Yards,
        "kilocalorie": ActivityStatisticUnit.Kilocalories,
        "bpm": ActivityStatisticUnit.BeatsPerMinute,
        "stepsPerMinute": ActivityStatisticUnit.StepsPerMinute,
        "rpm": ActivityStatisticUnit.RevolutionsPerMinute,
        "watt": ActivityStatisticUnit.Watts
    }

    def __init__(self):
        self._activityHierarchy = requests.get("http://connect.garmin.com/proxy/activity-service-1.2/json/activity_types").json()["dictionary"]

    def _get_cookies(self, record=None, email=None, password=None):
        from tapiriik.auth.credential_storage import CredentialStore
        if record:
            cached = self._sessionCache.Get(record.ExternalID)
            if cached:
                return cached
            #  longing for C style overloads...
            password = CredentialStore.Decrypt(record.ExtendedAuthorization["Password"])
            email = CredentialStore.Decrypt(record.ExtendedAuthorization["Email"])
        params = {"login": "login", "login:loginUsernameField": email, "login:password": password, "login:signInButton": "Sign In", "javax.faces.ViewState": "j_id1"}
        preResp = requests.get("https://connect.garmin.com/signin")
        resp = requests.post("https://connect.garmin.com/signin", data=params, allow_redirects=False, cookies=preResp.cookies)
        if resp.status_code >= 500 and resp.status_code<600:
            raise APIException("Remote API failure")
        if resp.status_code != 302:  # yep
            raise APIException("Invalid login", block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
        if record:
            self._sessionCache.Set(record.ExternalID, preResp.cookies)
        return preResp.cookies

    def WebInit(self):
        self.UserAuthorizationURL = WEB_ROOT + reverse("auth_simple", kwargs={"service": self.ID})

    def Authorize(self, email, password):
        from tapiriik.auth.credential_storage import CredentialStore
        cookies = self._get_cookies(email=email, password=password)
        username = requests.get("http://connect.garmin.com/user/username", cookies=cookies).json()["username"]
        if not len(username):
            raise APIException("Unable to retrieve username", block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
        return (username, {}, {"Email": CredentialStore.Encrypt(email), "Password": CredentialStore.Encrypt(password)})


    def _resolveActivityType(self, act_type):
        # Mostly there are two levels of a hierarchy, so we don't really need this as the parent is included in the listing.
        # But maybe they'll change that some day?
        while act_type not in self._activityMappings:
            try:
                act_type = [x["parent"]["key"] for x in self._activityHierarchy if x["key"] == act_type][0]
            except IndexError:
                raise ValueError("Activity type not found in activity hierarchy")
        return self._activityMappings[act_type]

    def DownloadActivityList(self, serviceRecord, exhaustive=False):
        #http://connect.garmin.com/proxy/activity-search-service-1.0/json/activities?&start=0&limit=50
        cookies = self._get_cookies(record=serviceRecord)
        page = 1
        pageSz = 50
        activities = []
        exclusions = []
        while True:
            logger.debug("Req with " + str({"start": (page - 1) * pageSz, "limit": pageSz}))
            res = requests.get("http://connect.garmin.com/proxy/activity-search-service-1.0/json/activities", params={"start": (page - 1) * pageSz, "limit": pageSz}, cookies=cookies)
            res = res.json()["results"]
            if "activities" not in res:
                break  # No activities on this page - empty account.
            for act in res["activities"]:
                act = act["activity"]
                if "sumDistance" not in act:
                    exclusions.append(APIExcludeActivity("No distance", activityId=act["activityId"]))
                    continue
                activity = UploadedActivity()

                if "beginLatitude" not in act or "endLatitude" not in act or (act["beginLatitude"] is act["endLatitude"] and act["beginLongitude"] is act["endLongitude"]):
                    activity.Stationary = True
                else:
                    activity.Stationary = False

                try:
                    activity.TZ = pytz.timezone(act["activityTimeZone"]["key"])
                except pytz.exceptions.UnknownTimeZoneError:
                    activity.TZ = pytz.FixedOffset(float(act["activityTimeZone"]["offset"]) * 60)

                logger.debug("Name " + act["activityName"]["value"] + ":")
                if len(act["activityName"]["value"].strip()) and act["activityName"]["value"] != "Untitled":
                    activity.Name = act["activityName"]["value"]
                # beginTimestamp/endTimestamp is in UTC
                activity.StartTime = pytz.utc.localize(datetime.utcfromtimestamp(float(act["beginTimestamp"]["millis"])/1000))
                if "sumElapsedDuration" in act:
                    activity.EndTime = activity.StartTime + timedelta(0, round(float(act["sumElapsedDuration"]["value"])))
                elif "sumDuration" in act:
                    activity.EndTime = activity.StartTime + timedelta(minutes=float(act["sumDuration"]["minutesSeconds"].split(":")[0]), seconds=float(act["sumDuration"]["minutesSeconds"].split(":")[1]))
                else:
                    activity.EndTime = pytz.utc.localize(datetime.utcfromtimestamp(float(act["endTimestamp"]["millis"])/1000))
                logger.debug("Activity s/t " + str(activity.StartTime) + " on page " + str(page))
                activity.AdjustTZ()
                # TODO: fix the distance stats to account for the fact that this incorrectly reported km instead of meters for the longest time.
                activity.Stats.Distance = ActivityStatistic(self._unitMap[act["sumDistance"]["uom"]], value=float(act["sumDistance"]["value"]))

                def mapStat(gcKey, statKey, type, useSourceUnits=False):
                    nonlocal activity, act
                    if gcKey in act:
                        value = float(act[gcKey]["value"])
                        if math.isinf(value):
                            return # GC returns the minimum speed as "-Infinity" instead of 0 some times :S
                        activity.Stats.__dict__[statKey].update(ActivityStatistic(self._unitMap[act[gcKey]["uom"]], **({type: value})))
                        if useSourceUnits:
                            activity.Stats.__dict__[statKey] = activity.Stats.__dict__[statKey].asUnits(self._unitMap[act[gcKey]["uom"]])


                if "sumMovingDuration" in act:
                    activity.Stats.MovingTime = ActivityStatistic(ActivityStatisticUnit.Time, value=timedelta(seconds=float(act["sumMovingDuration"]["value"])))


                mapStat("minSpeed", "Speed", "min", useSourceUnits=True) # We need to suppress conversion here, so we can fix the pace-speed issue below
                mapStat("maxSpeed", "Speed", "max", useSourceUnits=True)
                mapStat("weightedMeanSpeed", "Speed", "avg", useSourceUnits=True)
                mapStat("minAirTemperature", "Temperature", "min")
                mapStat("maxAirTemperature", "Temperature", "max")
                mapStat("weightedMeanAirTemperature", "Temperature", "avg")
                mapStat("sumEnergy", "Energy", "value")
                mapStat("maxHeartRate", "HR", "max")
                mapStat("weightedMeanHeartRate", "HR", "avg")
                mapStat("maxRunCadence", "RunCadence", "max")
                mapStat("weightedMeanRunCadence", "RunCadence", "avg")
                mapStat("maxBikeCadence", "Cadence", "max")
                mapStat("weightedMeanBikeCadence", "Cadence", "avg")
                mapStat("minPower", "Power", "min")
                mapStat("maxPower", "Power", "max")
                mapStat("weightedMeanPower", "Power", "avg")
                mapStat("minElevation", "Elevation", "min")
                mapStat("maxElevation", "Elevation", "max")
                mapStat("gainElevation", "Elevation", "gain")
                mapStat("lossElevation", "Elevation", "loss")

                # In Garmin Land, max can be smaller than min for this field :S
                if activity.Stats.Power.Max is not None and activity.Stats.Power.Min is not None and activity.Stats.Power.Min > activity.Stats.Power.Max:
                    activity.Stats.Power.Min = None

                # To get it to match what the user sees in GC.
                if activity.Stats.RunCadence.Max is not None:
                    activity.Stats.RunCadence.Max *= 2
                if activity.Stats.RunCadence.Average is not None:
                    activity.Stats.RunCadence.Average *= 2

                # GC incorrectly reports pace measurements as kph/mph when they are in fact in min/km or min/mi
                if "minSpeed" in act:
                    if ":" in act["minSpeed"]["withUnitAbbr"] and activity.Stats.Speed.Min:
                        activity.Stats.Speed.Min = 60 / activity.Stats.Speed.Min
                if "maxSpeed" in act:
                    if ":" in act["maxSpeed"]["withUnitAbbr"] and activity.Stats.Speed.Max:
                        activity.Stats.Speed.Max = 60 / activity.Stats.Speed.Max
                if "weightedMeanSpeed" in act:
                    if ":" in act["weightedMeanSpeed"]["withUnitAbbr"] and activity.Stats.Speed.Average:
                        activity.Stats.Speed.Average = 60 / activity.Stats.Speed.Average

                activity.Type = self._resolveActivityType(act["activityType"]["key"])

                activity.CalculateUID()
                activity.ServiceData = {"ActivityID": act["activityId"]}

                activities.append(activity)
            logger.debug("Finished page " + str(page) + " of " + str(res["search"]["totalPages"]))
            if not exhaustive or int(res["search"]["totalPages"]) == page:
                break
            else:
                page += 1
        return activities, exclusions

    def DownloadActivity(self, serviceRecord, activity):
        #http://connect.garmin.com/proxy/activity-service-1.1/tcx/activity/#####?full=true
        activityID = activity.ServiceData["ActivityID"]
        cookies = self._get_cookies(record=serviceRecord)
        res = requests.get("http://connect.garmin.com/proxy/activity-service-1.1/tcx/activity/" + str(activityID) + "?full=true", cookies=cookies)
        try:
            TCXIO.Parse(res.content, activity)
        except ValueError as e:
            raise APIExcludeActivity("TCX parse error " + str(e))
        if len(activity.Laps) == 1:
            activity.Laps[0].Stats.update(activity.Stats) # I trust Garmin Connect's stats more than whatever shows up in the TCX
            activity.Stats = activity.Laps[0].Stats # They must be identical to pass the verification
        return activity

    def UploadActivity(self, serviceRecord, activity):
        #/proxy/upload-service-1.1/json/upload/.fit
        activity.EnsureTZ()
        fit_file = FITIO.Dump(activity)
        files = {"data": ("tap-sync-" + str(os.getpid()) + "-" + activity.UID + ".fit", fit_file)}
        cookies = self._get_cookies(record=serviceRecord)
        res = requests.post("http://connect.garmin.com/proxy/upload-service-1.1/json/upload/.tcx", files=files, cookies=cookies)
        res = res.json()["detailedImportResult"]

        if len(res["successes"]) == 0:
            raise APIException("Unable to upload activity")
        if len(res["successes"]) > 1:
            raise APIException("Uploaded succeeded, resulting in too many activities")
        actid = res["successes"][0]["internalId"]

        if activity.Name:
            res = requests.post("http://connect.garmin.com/proxy/activity-service-1.2/json/name/" + str(actid), data={"value": activity.Name}, cookies=cookies)
            res = res.json()
            if "display" not in res or res["display"]["value"] != activity.Name:
                raise APIWarning("Unable to set activity name")

        if activity.Notes:
            res = requests.post("http://connect.garmin.com/proxy/activity-service-1.2/json/description/" + str(actid), data={"value": activity.Notes}, cookies=cookies)
            res = res.json()
            if "display" not in res or res["display"]["value"] != activity.Notes:
                raise APIWarning("Unable to set activity notes")

        if activity.Type not in [ActivityType.Running, ActivityType.Cycling, ActivityType.Other]:
            # Set the legit activity type - whatever it is, it's not supported by the TCX schema
            acttype = [k for k, v in self._reverseActivityMappings.items() if v == activity.Type]
            if len(acttype) == 0:
                raise APIWarning("GarminConnect does not support activity type " + activity.Type)
            else:
                acttype = acttype[0]
            res = requests.post("http://connect.garmin.com/proxy/activity-service-1.2/json/type/" + str(actid), data={"value": acttype}, cookies=cookies)
            res = res.json()
            if "activityType" not in res or res["activityType"]["key"] != acttype:
                raise APIWarning("Unable to set activity type")



    def RevokeAuthorization(self, serviceRecord):
        # nothing to do here...
        pass

    def DeleteCachedData(self, serviceRecord):
        # nothing cached...
        pass
