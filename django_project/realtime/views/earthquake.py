# coding=utf-8
import logging
from copy import deepcopy

from django.conf import settings
from django.core.exceptions import ValidationError, MultipleObjectsReturned
from django.db.utils import IntegrityError
from django.http.response import HttpResponseBadRequest, JsonResponse, \
    HttpResponse
from django.utils.translation import ugettext as _
from django.utils import translation
from django.shortcuts import render_to_response
from django.template import RequestContext
from realtime.helpers.rest_push_indicator import track_rest_push
from rest_framework import status, mixins
from rest_framework.filters import DjangoFilterBackend, SearchFilter, \
    OrderingFilter
from rest_framework.generics import GenericAPIView
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser
from rest_framework.permissions import DjangoModelPermissionsOrAnonReadOnly
from rest_framework.response import Response
from realtime.app_settings import (
    LEAFLET_TILES, LANGUAGE_LIST, MAPQUEST_MAP_KEY)
from realtime.forms import FilterForm
from realtime.filters.earthquake_filter import EarthquakeFilter
from realtime.models.earthquake import Earthquake, EarthquakeReport
from realtime.serializers.earthquake_serializer import EarthquakeSerializer, \
    EarthquakeReportSerializer, EarthquakeGeoJsonSerializer
from rest_framework_gis.filters import InBBoxFilter

from realtime.tasks.earthquake import push_shake_to_inaware
from realtime.tasks.realtime.earthquake import process_shake

__author__ = 'Rizky Maulana Nugraha "lucernae" <lana.pcfre@gmail.com>'
__date__ = '19/06/15'


LOGGER = logging.getLogger(__name__)


def index(request, iframe=False, server_side_filter=False):
    """Index page of realtime.

    :param request: A django request object.
    :type request: request

    :returns: Response will be a leaflet map page.
    :rtype: HttpResponse
    """

    if request.method == 'POST':
        pass
    else:
        form = FilterForm()

    language_code = 'en'
    if request.method == 'GET':
        if 'iframe' in request.GET:
            iframe = request.GET.get('iframe')
        if 'server_side_filter' in request.GET:
            server_side_filter = request.GET.get('server_side_filter')
        if 'lang' in request.GET:
            language_code = request.GET.get('lang')

    leaflet_tiles = []
    for i in range(0, len(LEAFLET_TILES[1])):
        leaflet_tiles.append(
            dict(
                name=LEAFLET_TILES[0][i],
                url=LEAFLET_TILES[1][i],
                subdomains=LEAFLET_TILES[2][i],
                attribution=LEAFLET_TILES[3][i]
            )
        )

    context = RequestContext(request)
    context['leaflet_tiles'] = leaflet_tiles
    selected_language = {
        'id': 'en',
        'name': 'English'
    }
    for l in LANGUAGE_LIST:
        if l['id'] == language_code:
            selected_language = l

    language_list = [l for l in LANGUAGE_LIST if not l['id'] == language_code]
    context['language'] = {
        'selected_language': selected_language,
        'language_list': language_list,
    }
    translation.activate(selected_language['id'])
    request.session[translation.LANGUAGE_SESSION_KEY] = \
        selected_language['id']
    context['select_area_text'] = _('Select Area')
    context['remove_area_text'] = _('Remove Selection')
    context['select_current_zoom_text'] = _('Select area within current zoom')
    context['iframe'] = iframe
    context['mapquest_key'] = MAPQUEST_MAP_KEY
    return render_to_response(
        'realtime/earthquake/index.html',
        {
            'form': form,
            'iframe': iframe,
            'server_side_filter': server_side_filter
        },
        context_instance=context)


def iframe_index(request):
    """Index page of realtime in iframe.

    :param request: A django request object.
    :type request: request

    :returns: Response will be a leaflet map page.
    :rtype: HttpResponse
    """
    return index(request, iframe=True)


class EarthquakeList(mixins.ListModelMixin, mixins.CreateModelMixin,
                     GenericAPIView):
    """
    Provides GET and POST requests to retrieve and save Earthquake models.

    ### Filters

    These are the available filters:

    * min_depth
    * max_depth
    * min_magnitude or minimum_magnitude
    * max_magnitude or maximum_magnitude
    * min_time or time_start
    * max_time or time_end
    * location_description
    * in_bbox filled with BBox String in the format SWLon,SWLat,NELon,NELat
    this is used as geographic box filter
    """

    queryset = Earthquake.objects.all()
    serializer_class = EarthquakeSerializer
    # parser_classes = [JSONParser, FormParser]
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter,
                       InBBoxFilter)
    bbox_filter_field = 'location'
    bbox_filter_include_overlapping = True
    filter_fields = ('depth', 'magnitude', 'shake_id')
    filter_class = EarthquakeFilter
    search_fields = ('location_description', )
    ordering = ('shake_id', )
    permission_classes = (DjangoModelPermissionsOrAnonReadOnly, )

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        retval = self.create(request, *args, **kwargs)
        track_rest_push(request)
        if not settings.DEV_MODE:
            # carefuly DO NOT push it to InaWARE when in dev_mode
            push_shake_to_inaware.delay(request.data.get('shake_id'))
        return retval


class EarthquakeDetail(mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                       mixins.DestroyModelMixin, GenericAPIView):
    """
    Provides GET, PUT, and DELETE requests to retrieve, update and delete
    Earthquake models.
    """
    queryset = Earthquake.objects.all()
    serializer_class = EarthquakeSerializer
    lookup_field = 'shake_id'
    permission_classes = (DjangoModelPermissionsOrAnonReadOnly, )

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        try:
            data = request.data
            shake_id = kwargs.get('shake_id') or data.get('shake_id')
            instance = Earthquake.objects.get(shake_id=shake_id)
            if instance.shake_grid:
                instance.shake_grid.delete()
            if 'shake_grid' in request.data:
                # posting shake grid means only updating its shake_grid
                # properties
                request.data['shake_id'] = shake_id
                request.data['location'] = instance.location
                request.data['location_description'] = \
                    instance.location_description
                request.data['time'] = instance.time
                request.data['magnitude'] = instance.magnitude
                request.data['depth'] = instance.depth
        except Earthquake.DoesNotExist:
            pass
        retval = self.update(request, *args, **kwargs)
        track_rest_push(request)
        return retval

    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)


class EarthquakeReportList(mixins.ListModelMixin,
                           mixins.RetrieveModelMixin,
                           mixins.CreateModelMixin,
                           GenericAPIView):
    """
    Provides GET and POST requests to retrieve and save Earthquake
    Report models.

    ### Filters

    These are the available filters:

    * earthquake__shake_id
    * language
    """
    queryset = EarthquakeReport.objects.all()
    serializer_class = EarthquakeReportSerializer
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filter_fields = ('earthquake__shake_id', 'language', )
    search_fields = ('earthquake__shake_id', 'language', )
    ordering_fields = ('earthquake__shake_id', 'language', )
    ordering = ('earthquake__shake_id', )
    permission_classes = (DjangoModelPermissionsOrAnonReadOnly, )

    def get(self, request, shake_id=None, *args, **kwargs):
        try:
            if shake_id:
                instances = EarthquakeReport.objects.filter(
                    earthquake__shake_id=shake_id)
                page = self.paginate_queryset(instances)
                if page is not None:
                    serializer = self.get_serializer(page, many=True)
                    return self.get_paginated_response(serializer.data)

                serializer = self.get_serializer(instances, many=True)
                return Response(serializer.data)
            else:
                return self.list(request, *args, **kwargs)
        except EarthquakeReport.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

    def post(self, request, *args, **kwargs):
        data = request.data
        try:
            shake_id = kwargs.get('shake_id') or data.get('shake_id')
            data['shake_id'] = shake_id
            earthquake = Earthquake.objects.get(shake_id=shake_id)
            report = EarthquakeReport.objects.filter(
                earthquake=earthquake, language=data['language'])
        except Earthquake.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if report:
            # cannot post report if it is already exists
            serializer = EarthquakeReportSerializer(report[0])
            return Response(
                serializer.data, status=status.HTTP_400_BAD_REQUEST)

        serializer = EarthquakeReportSerializer(data=data)

        if serializer.is_valid():
            serializer.validated_data['earthquake'] = earthquake
            try:
                serializer.save()
                return Response(
                    serializer.data, status=status.HTTP_201_CREATED)
            except (ValidationError, IntegrityError) as e:
                # This happens when simultaneuously two conn trying to save
                # the same unique_together fields (earthquake, language)
                # Should warn this to sentry
                LOGGER.warning(e.message)
                return Response(
                    serializer.errors,
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class EarthquakeReportDetail(mixins.ListModelMixin,
                             mixins.RetrieveModelMixin,
                             mixins.UpdateModelMixin,
                             mixins.DestroyModelMixin, GenericAPIView):
    """
    Provides GET, PUT, and DELETE requests to retrieve, update and delete
    Earthquake Report models.
    """
    queryset = EarthquakeReport.objects.all()
    serializer_class = EarthquakeReportSerializer
    parser_classes = (JSONParser, FormParser, MultiPartParser, )
    permission_classes = (DjangoModelPermissionsOrAnonReadOnly, )

    def get(self, request, shake_id=None, language=None, *args, **kwargs):
        try:
            if shake_id and language:
                instance = EarthquakeReport.objects.get(
                    earthquake__shake_id=shake_id,
                    language=language)
                serializer = self.get_serializer(instance)
                return Response(serializer.data)
            elif shake_id:
                return self.list(request, *args, **kwargs)
        except EarthquakeReport.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except MultipleObjectsReturned as e:
            # this should not happen.
            # But in case it is happening, returned the last object, but still
            # log the error to sentry
            LOGGER.warning(e.message)
            instance = EarthquakeReport.objects.filter(
                earthquake__shake_id=shake_id,
                language=language).last()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)

    def put(self, request, shake_id=None, language=None):
        data = request.data
        try:
            if shake_id:
                data['shake_id'] = shake_id
                report = EarthquakeReport.objects.get(
                    earthquake__shake_id=shake_id, language=language)
            else:
                return Response(status=status.HTTP_400_BAD_REQUEST)
        except EarthquakeReport.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        # delete previous files
        new_report = deepcopy(report)
        new_report.pk = None
        report.delete()
        serializer = EarthquakeReportSerializer(new_report, data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, shake_id, language):
        try:
            report = EarthquakeReport.objects.get(
                earthquake__shake_id=shake_id, language=language)
        except EarthquakeReport.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        report.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class EarthquakeFeatureList(EarthquakeList):
    """
    Provides GET requests to retrieve Earthquake models
    in a GEOJSON format.

    ### Filters

    These are the available filters:

    * min_depth
    * max_depth
    * min_magnitude or minimum_magnitude
    * max_magnitude or maximum_magnitude
    * min_time or time_start
    * max_time or time_end
    * location_description
    * felt shakes
    * in_bbox filled with BBox String in the format SWLon,SWLat,NELon,NELat
    this is used as geographic box filter
    """
    serializer_class = EarthquakeGeoJsonSerializer
    pagination_class = None


def get_grid_xml(request, shake_id):
    if request.method != 'GET':
        return HttpResponseBadRequest()

    try:
        shake = Earthquake.objects.get(shake_id=shake_id)
        if not shake.shake_grid:
            # fetch shake grid from Realtime Processor
            process_shake.delay(shake_id)
            return JsonResponse({'success': True})
        response = HttpResponse(
            shake.shake_grid.read(), content_type='application/octet-stream')
        response['Content-Disposition'] = \
            'inline; filename="%s-grid.xml"' % shake_id

        return response
    except:
        return HttpResponseBadRequest()


def trigger_process_shake(request, shake_id):
    if request.method != 'POST':
        return HttpResponseBadRequest()

    try:
        process_shake.delay(shake_id)
        return JsonResponse({'success': True})
    except:
        return HttpResponseBadRequest()
