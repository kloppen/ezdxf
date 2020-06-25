# Created: 06.2020
# Copyright (c) 2020, Matthew Broadway
# License: MIT License
import copy
import math
from math import radians
from typing import Set, Iterable, cast, Union, List

from ezdxf.addons.drawing.backend_interface import DrawingBackend
from ezdxf.addons.drawing.properties import RenderContext, VIEWPORT_COLOR
from ezdxf.addons.drawing.text import simplified_text_chunks
from ezdxf.addons.drawing.utils import normalize_angle, get_rotation_direction_from_extrusion_vector, \
    get_draw_angles, get_tri_or_quad_points
from ezdxf.entities import DXFGraphic, Insert, MText, Dimension, Polyline, LWPolyline, Face3d, Mesh, Solid, Trace, \
    Spline, Hatch, Attrib, Text
from ezdxf.entities.dxfentity import DXFTagStorage
from ezdxf.layouts import Layout
from ezdxf.math import Vector

INFINITE_LINE_LENGTH = 25

LINE_ENTITY_TYPES = {'LINE', 'XLINE', 'RAY', 'MESH'}
TEXT_ENTITY_TYPES = {'TEXT', 'MTEXT', 'ATTRIB'}
CURVE_ENTITY_TYPES = {'CIRCLE', 'ARC', 'ELLIPSE', 'SPLINE'}
MISC_ENTITY_TYPES = {'POINT', '3DFACE', 'SOLID', 'TRACE', 'MESH', 'HATCH', 'VIEWPORT'}
COMPOSITE_ENTITY_TYPES = {'INSERT', 'POLYLINE', 'LWPOLYLINE'}  # and DIMENSION*


def _draw_line_entity(entity: DXFGraphic, ctx: RenderContext, out: DrawingBackend) -> None:
    d, dxftype = entity.dxf, entity.dxftype()
    properties = ctx.resolve_all(entity)
    if dxftype == 'LINE':
        out.draw_line(d.start, d.end, properties)

    elif dxftype in ('XLINE', 'RAY'):
        start = d.start
        delta = Vector(d.unit_vector.x, d.unit_vector.y, 0) * INFINITE_LINE_LENGTH
        if dxftype == 'XLINE':
            out.draw_line(start - delta / 2, start + delta / 2, properties)
        elif dxftype == 'RAY':
            out.draw_line(start, start + delta, properties)

    elif dxftype == 'MESH':
        entity = cast(Mesh, entity)
        data = entity.get_data()  # unpack into more readable format
        points = [Vector(x, y, z) for x, y, z in data.vertices]
        for a, b in data.edges:
            out.draw_line(points[a], points[b], properties)

    else:
        raise TypeError(dxftype)


def _draw_text_entity(entity: DXFGraphic, ctx: RenderContext, out: DrawingBackend) -> None:
    d, dxftype = entity.dxf, entity.dxftype()
    # todo: how to handle text placed in 3D (extrusion != (0, 0, [1, -1]))
    properties = ctx.resolve_all(entity)
    if dxftype in ('TEXT', 'MTEXT', 'ATTRIB'):
        entity = cast(Union[Text, MText, Attrib], entity)
        for line, transform, cap_height in simplified_text_chunks(entity, out):
            out.draw_text(line, transform, properties, cap_height)
    else:
        raise TypeError(dxftype)


def _get_arc_wcs_center(arc: DXFGraphic) -> Vector:
    """ Returns the center of an ARC or CIRCLE as WCS coordinates. """
    center = arc.dxf.center
    if arc.dxf.hasattr('extrusion'):
        ocs = arc.ocs()
        return ocs.to_wcs(center)
    else:
        return center


def _draw_curve_entity(entity: DXFGraphic, ctx: RenderContext, out: DrawingBackend) -> None:
    # todo: how to handle ARC and CIRCLE placed in 3D (extrusion != (0, 0, [1, -1]))
    d, dxftype = entity.dxf, entity.dxftype()
    properties = ctx.resolve_all(entity)
    if dxftype == 'CIRCLE':
        center = _get_arc_wcs_center(entity)
        diameter = 2 * d.radius
        out.draw_arc(center, diameter, diameter, 0, None, properties)

    elif dxftype == 'ARC':
        center = _get_arc_wcs_center(entity)
        diameter = 2 * d.radius
        direction = get_rotation_direction_from_extrusion_vector(d.extrusion)
        draw_angles = get_draw_angles(direction, radians(d.start_angle), radians(d.end_angle))
        out.draw_arc(center, diameter, diameter, 0, draw_angles, properties)

    elif dxftype == 'ELLIPSE':
        # 'param' angles are anticlockwise around the extrusion vector
        # 'param' angles are relative to the major axis angle
        # major axis angle always anticlockwise in global frame
        major_axis_angle = normalize_angle(math.atan2(d.major_axis.y, d.major_axis.x))
        width = 2 * d.major_axis.magnitude
        height = d.ratio * width  # ratio == height / width
        direction = get_rotation_direction_from_extrusion_vector(d.extrusion)
        draw_angles = get_draw_angles(direction, d.start_param, d.end_param)
        out.draw_arc(d.center, width, height, major_axis_angle, draw_angles, properties)

    elif dxftype == 'SPLINE':
        entity = cast(Spline, entity)
        spline = entity.construction_tool()
        if out.has_spline_support:
            out.draw_spline(spline, properties)
        else:
            points = list(spline.approximate(segments=100))
            out.start_polyline()
            for a, b in zip(points, points[1:]):
                out.draw_line(a, b, properties)
            out.end_polyline()

    else:
        raise TypeError(dxftype)


def _draw_misc_entity(entity: DXFGraphic, ctx: RenderContext, out: DrawingBackend) -> None:
    d, dxftype = entity.dxf, entity.dxftype()
    properties = ctx.resolve_all(entity)
    if dxftype == 'POINT':
        out.draw_point(d.location, properties)

    elif dxftype in ('3DFACE', 'SOLID', 'TRACE'):
        # TRACE is the same thing as SOLID according to the documentation
        # https://ezdxf.readthedocs.io/en/stable/dxfentities/trace.html
        # except TRACE has OCS coordinates and SOLID has WCS coordinates.
        entity = cast(Union[Face3d, Solid, Trace], entity)
        points = get_tri_or_quad_points(entity)
        if dxftype == 'TRACE' and d.hasattr('extrusion'):
            ocs = entity.ocs()
            points = list(ocs.points_to_wcs(points))
        if dxftype in ('SOLID', 'TRACE'):
            out.draw_filled_polygon(points, properties)
        else:
            for a, b in zip(points, points[1:]):
                out.draw_line(a, b, properties)

    elif dxftype == 'HATCH':
        entity = cast(Hatch, entity)
        ocs = entity.ocs()
        # all OCS coordinates have the same z-axis stored as vector (0, 0, z), default (0, 0, 0)
        elevation = entity.dxf.elevation.z
        paths = copy.deepcopy(entity.paths)
        paths.polyline_to_edge_path(just_with_bulge=False)
        paths.all_to_line_edges(spline_factor=10)
        for p in paths:
            assert p.PATH_TYPE == 'EdgePath'
            vertices = []
            last_vertex = None
            for e in p.edges:
                assert e.EDGE_TYPE == 'LineEdge'
                # WCS transformation is only done if the extrusion vector is != (0, 0, 1)
                # else to_wcs() returns just the input - no big speed penalty!
                v = ocs.to_wcs(Vector(e.start[0], e.start[1], elevation))
                if last_vertex is not None and not last_vertex.isclose(v):
                    print(f'warning: hatch edges not contiguous: {last_vertex} -> {e.start}, {e.end}')
                    vertices.append(last_vertex)
                vertices.append(v)
                last_vertex = ocs.to_wcs(Vector(e.end[0], e.end[1], elevation)).replace(z=0.0)
            if vertices:
                if last_vertex.isclose(vertices[0]):
                    vertices.append(last_vertex)
                out.draw_filled_polygon(vertices, properties)

    elif dxftype == 'VIEWPORT':
        view_vector: Vector = d.view_direction_vector
        mag = view_vector.magnitude
        if math.isclose(mag, 0.0):
            print('warning: viewport with null view vector')
            return
        view_vector /= mag
        if not math.isclose(view_vector.dot(Vector(0, 0, 1)), 1.0):
            print(f'cannot render viewport with non-perpendicular view direction: {d.view_direction_vector}')
            return

        cx, cy = d.center.x, d.center.y
        dx = d.width / 2
        dy = d.height / 2
        minx, miny = cx - dx, cy - dy
        maxx, maxy = cx + dx, cy + dy
        points = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
        out.draw_filled_polygon([Vector(x, y, 0) for x, y in points], VIEWPORT_COLOR)

    else:
        raise TypeError(dxftype)


def _draw_composite_entity(entity: DXFGraphic, ctx: RenderContext,
                           out: DrawingBackend, parent_stack: List[DXFGraphic]) -> None:
    dxftype = entity.dxftype()

    if dxftype == 'INSERT':
        entity = cast(Insert, entity)
        ctx.push_state(ctx.resolve_all(entity))
        parent_stack.append(entity)
        for attrib in entity.attribs:
            draw_entity(attrib, ctx, out, parent_stack)
        try:
            children = list(entity.virtual_entities())
        except Exception as e:
            print(f'Exception {type(e)}({e}) failed to get children of insert entity: {e}')
            return
        for child in children:
            draw_entity(child, ctx, out, parent_stack)
        parent_stack.pop()
        ctx.pop_state()

    elif dxftype == 'DIMENSION':
        entity = cast(Dimension, entity)
        children = []
        try:
            for child in entity.virtual_entities():
                child.transparency = 0.0  # defaults to 1.0 (fully transparent)
                children.append(child)
        except Exception as e:
            print(f'Exception {type(e)}({e}) failed to get children of dimension entity: {e}')
            return

        parent_stack.append(entity)
        for child in children:
            draw_entity(child, ctx, out, parent_stack)
        parent_stack.pop()

    elif dxftype in ('LWPOLYLINE', 'POLYLINE'):
        entity = cast(Union[LWPolyline, Polyline], entity)
        parent_stack.append(entity)
        out.start_polyline()
        for child in entity.virtual_entities():
            draw_entity(child, ctx, out, parent_stack)
        parent_stack.pop()

        out.set_current_entity(entity, tuple(parent_stack))
        out.end_polyline()
        out.set_current_entity(None)

    else:
        raise TypeError(dxftype)


def draw_entity(entity: DXFGraphic, ctx: RenderContext, out: DrawingBackend, parent_stack: List[DXFGraphic]) -> None:
    dxftype = entity.dxftype()
    out.set_current_entity(entity, tuple(parent_stack))
    if dxftype in LINE_ENTITY_TYPES:
        _draw_line_entity(entity, ctx, out)
    elif dxftype in TEXT_ENTITY_TYPES:
        _draw_text_entity(entity, ctx, out)
    elif dxftype in CURVE_ENTITY_TYPES:
        _draw_curve_entity(entity, ctx, out)
    elif dxftype in MISC_ENTITY_TYPES:
        _draw_misc_entity(entity, ctx, out)
    elif dxftype in COMPOSITE_ENTITY_TYPES:
        _draw_composite_entity(entity, ctx, out, parent_stack)
    else:
        out.ignored_entity(entity)
    out.set_current_entity(None)


def draw_entities(entities: Iterable[DXFGraphic], ctx: RenderContext, out: DrawingBackend) -> None:
    for entity in entities:
        if isinstance(entity, DXFTagStorage):
            print(f'ignoring unsupported DXF entity: {str(entity)}')
            # unsupported DXF entity, just tag storage to preserve data
            continue
        if ctx.is_visible(entity):
            draw_entity(entity, ctx, out, [])


def draw_layout(layout: 'Layout', ctx: RenderContext, out: DrawingBackend, visible_layers: Set[str] = None, finalize: bool = True) -> None:
    """

    Args:
        layout: layout to draw
        ctx: actual render context of a DXF document
        out:  backend
        visible_layers: alternative layer state independent from DXF layer state,
                        existing layer names from this set are visible all other layers
                        are invisible, if `None` the usual DXF layer state is significant.
        finalize: finalize backend automatically

    """
    ctx.set_visible_layers(visible_layers)
    entities = layout.entity_space
    draw_entities(entities, ctx, out)
    out.set_background(ctx.current_layout.background_color)
    if finalize:
        out.finalize()