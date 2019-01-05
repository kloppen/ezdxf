# Created: 28.12.2018
# Copyright (C) 2018-2019, Manfred Moitzi
# License: MIT License
from typing import TYPE_CHECKING, Tuple, Iterable, Any
import math
from ezdxf.algebra import Vector, Ray2D, xround
from ezdxf.algebra import UCS, PassTroughUCS
from ezdxf.lldxf.const import DXFValueError, DXFUndefinedBlockError, DXFAttributeError
from ezdxf.options import options
from ezdxf.modern.tableentries import DimStyle  # DimStyle for DXF R2000 and later
from ezdxf.tools import suppress_zeros, raise_decimals
from ezdxf.render.arrows import ARROWS

if TYPE_CHECKING:
    from ezdxf.eztypes import Dimension, BlockLayout, Vertex

DIMSTYLE_CHECKER = DimStyle.new('0', dxfattribs={'name': 'DIMSTYLE_CHECKER'})


class DimStyleOverride:
    def __init__(self, dim_style: 'DimStyle', override: dict = None):
        self.dim_style = dim_style
        self.override = override or {}
        self._cache = {}

    def get(self, attribute: str, default: Any = None) -> Any:
        try:
            return self._cache[attribute]
        except KeyError:
            pass
        # has to be at least a valid DXF R2000 attribute
        if not DIMSTYLE_CHECKER.supports_dxf_attrib(attribute):
            raise DXFAttributeError('Invalid DXF attribute "{}" for DIMSTYLE.'.format(attribute))

        if attribute in self.override:
            result = self.override[attribute]
        else:
            # Return default value for attributes not supported by DXF R12.
            # This is a hack to use the same algorithm to render DXF R2000 and DXF R12 DIMENSION entities.
            # But the DXF R2000 attributes are not stored in the DXF R12 file!!!
            try:
                result = self.dim_style.get_dxf_attrib(attribute, default)
            except DXFAttributeError:
                # return default value for DXF R12 if valid DXF R2000 attribute
                result = default
        self._cache[attribute] = result
        return result

    def set_acad_dstyle(self, dimension: 'Dimension') -> None:
        dimension.set_acad_dstyle(self.override, DIMSTYLE_CHECKER)


class DimensionBase:
    def __init__(self, dimension: 'Dimension', dim_style: 'DimStyle', block: 'BlockLayout', ucs: 'UCS' = None,
                 override: dict = None):
        self.drawing = dimension.drawing
        self.dxfversion = self.drawing.dxfversion
        self.block = block
        self.dim_style = DimStyleOverride(dim_style, override)
        self.text_style = self.dim_style.get('dimtxsty', options.default_dimension_text_style)
        self.dimension = dimension
        self.ucs = ucs or PassTroughUCS()
        self.requires_extrusion = self.ucs.uz != (0, 0, 1)
        if self.requires_extrusion:  # set extrusion vector of DIMENSION entity
            self.dimension.dxf.extrusion = self.ucs.uz
        # write override values into dimension entity XDATA section
        self.dim_style.set_acad_dstyle(self.dimension)

    @property
    def text_height(self) -> float:
        return self.dim_style.get('dimtxt', 1.0)

    @property
    def suppress_extension_line1(self) -> bool:
        return bool(self.dim_style.get('dimse1', False))

    @property
    def suppress_extension_line2(self) -> bool:
        return bool(self.dim_style.get('dimse2', False))

    def default_attributes(self) -> dict:
        return {
            'layer': self.dimension.dxf.layer,
            'color': self.dimension.dxf.color,
        }

    def wcs(self, point: 'Vertex') -> Vector:
        return self.ucs.to_wcs(point)

    def ocs(self, point: 'Vertex') -> Vector:
        return self.ucs.to_ocs(point)

    def get_text(self, measurement: float) -> str:
        text = self.dimension.dxf.text
        if text == ' ':  # suppress text
            return ''
        elif text == '' or text == '<>':  # measured distance
            return self.format_text(measurement)
        else:  # user override
            return text

    def get_arrow_names(self) -> Tuple[str, str]:
        get_dxf_attr = self.dim_style.get
        dimtsz = get_dxf_attr('dimtsz')
        blk1, blk2 = None, None
        if dimtsz == 0.:  # oblique stroke, but double the size
            if bool(get_dxf_attr('dimsah')):
                blk1 = get_dxf_attr('dimblk1')
                blk2 = get_dxf_attr('dimblk2')
            else:
                blk = get_dxf_attr('dimblk')
                blk1 = blk
                blk2 = blk
        return blk1, blk2

    def format_text(self, value: float) -> str:
        dimrnd = self.dim_style.get('dimrnd', None)
        dimdec = self.dim_style.get('dimdec', None)
        dimzin = self.dim_style.get('dimzin', 0)
        dimdsep = self.dim_style.get('dimdsep', '.')
        dimpost = self.dim_style.get('dimpost', '<>')
        return format_text(value, dimrnd, dimdec, dimzin, dimdsep, dimpost)

    def add_line(self, start: 'Vertex', end: 'Vertex', dxfattribs: dict = None) -> None:
        attribs = self.default_attributes()
        if dxfattribs:
            attribs.update(dxfattribs)
        self.block.add_line(self.wcs(start), self.wcs(end), dxfattribs=attribs)

    def add_blockref(self, name: str, insert: 'Vertex', rotation: float = 0,
                     scale: float = 1., reverse=False, dxfattribs: dict = None) -> Vector:
        if name in ARROWS:  # generates automatically BLOCK definitions for arrows if needed
            return self.block.add_arrow_blockref(name, insert=insert, size=scale, rotation=rotation, reverse=reverse,
                                                 dxfattribs=dxfattribs)
        else:
            if name not in self.drawing.blocks:
                raise DXFUndefinedBlockError('Undefined block: "{}"'.format(name))

            attribs = self.default_attributes()
            attribs['rotation'] = rotation
            if scale != 1.:
                attribs['xscale'] = scale
                attribs['yscale'] = scale
            if self.requires_extrusion:
                attribs['extrusion'] = self.ucs.uz
            if dxfattribs:
                attribs.update(dxfattribs)
            self.block.add_blockref(name, insert=self.ocs(insert), dxfattribs=attribs)
            return insert

    def add_text(self, text: str, pos: 'Vertex', rotation: float, dxfattribs: dict = None) -> None:
        attribs = self.default_attributes()
        attribs['rotation'] = rotation
        attribs['style'] = self.text_style
        attribs['height'] = self.text_height
        if dxfattribs:
            attribs.update(dxfattribs)
        dxftext = self.block.add_text(text, dxfattribs=attribs)
        dxftext.set_pos(self.ocs(pos), align='MIDDLE_CENTER')

    def add_defpoints(self, points: Iterable['Vertex']) -> None:
        attribs = {
            'layer': 'DEFPOINTS',
        }
        for point in points:
            self.block.add_point(self.wcs(point), dxfattribs=attribs)


class LinearDimension(DimensionBase):
    def render(self):
        dim = self.dimension.dxf

        angle = math.radians(dim.angle)
        ext_angle = angle + math.pi / 2.

        dimline_ray = Ray2D(dim.defpoint, angle=angle)
        ext1_ray = Ray2D(dim.defpoint2, angle=ext_angle)
        ext2_ray = Ray2D(dim.defpoint3, angle=ext_angle)
        dimline_start = dimline_ray.intersect(ext1_ray)
        dimline_end = dimline_ray.intersect(ext2_ray)
        dim.defpoint = dimline_start  # set defpoint to expected location
        dimlfac = self.dim_style.get('dimlfac', 1.)
        measurement = (dimline_start - dimline_end).magnitude
        dim_text = self.get_text(measurement * dimlfac)

        # add text
        if dim_text:
            pos = self.dimension.get_dxf_attrib('text_midpoint', None)
            # calculate text midpoint if unset
            if pos is None:
                pos = self.get_text_midpoint(dimline_start, dimline_end)
                self.dimension.set_dxf_attrib('text_midpoint', pos)

            self.add_measurement_text(dim_text, pos)

        # add extension line 1
        if not self.suppress_extension_line1:
            self.add_extension_line(dim.defpoint2, dimline_start)

        # add extension line 1
        if not self.suppress_extension_line2:
            self.add_extension_line(dim.defpoint3, dimline_end)

        blk1, blk2 = self.get_arrow_names()
        # add arrows
        dimline_start, dimline_end = self.add_arrows(dimline_start, dimline_end, blk1, blk2)
        self.add_dimension_line(dimline_start, dimline_end, blk1, blk2)

        # add POINT at definition points
        self.add_defpoints([dim.defpoint, dim.defpoint2, dim.defpoint3])
        self.defpoints_to_wcs()

    def defpoints_to_wcs(self):
        def from_ucs(attr, func):
            point = self.dimension.get_dxf_attrib(attr)
            self.dimension.set_dxf_attrib(attr, func(point))

        from_ucs('defpoint', self.wcs)
        from_ucs('defpoint2', self.wcs)
        from_ucs('defpoint3', self.wcs)
        from_ucs('text_midpoint', self.ocs)

    def add_measurement_text(self, dim_text: str, pos: Vector) -> None:
        attribs = {
            'color': self.dim_style.get('dimclrt', self.dimension.dxf.color)
        }
        angle = self.dimension.get_dxf_attrib('angle', 0)
        text_rotation = self.dimension.get_dxf_attrib('text_rotation', 0)
        self.add_text(dim_text, pos=pos, rotation=angle + text_rotation, dxfattribs=attribs)

    def add_dimension_line(self, start: 'Vertex', end: 'Vertex', blk1: str = None, blk2: str = None) -> None:
        direction = (end - start).normalize()
        extension = direction * self.dim_style.get('dimdle', 0.)
        if blk1 is None or ARROWS.has_extension_line(blk1):
            start = start - extension
        if blk2 is None or ARROWS.has_extension_line(blk2):
            end = end + extension
        # is dimension line crossing text
        attribs = {
            'color': self.dim_style.get('dimclrd', self.dimension.dxf.color)
        }
        self.add_line(start, end, dxfattribs=attribs)

    def add_extension_line(self, start: 'Vertex', end: 'Vertex') -> None:
        direction = (end - start).normalize()
        offset = self.dim_style.get('dimexo', 0.)
        extension = self.dim_style.get('dimexe', 0.)
        start = start + direction * offset
        end = end + direction * extension
        attribs = {
            'color': self.dim_style.get('dimclre', self.dimension.dxf.color)
        }
        self.add_line(start, end, dxfattribs=attribs)

    def add_arrows(self, start: 'Vertex', end: 'Vertex', blk1: str = None, blk2: str = None) -> Tuple[Vector, Vector]:
        dim = self.dimension.dxf
        get_dxf_attr = self.dim_style.get
        attribs = {
            'color': get_dxf_attr('dimclrd', self.dimension.dxf.color),
        }
        dimtsz = get_dxf_attr('dimtsz')
        if dimtsz > 0.:  # oblique stroke, but double the size
            self.block.add_arrow(ARROWS.oblique, insert=start, rotation=dim.angle, size=dimtsz * 2, dxfattribs=attribs)
            self.block.add_arrow(ARROWS.oblique, insert=end, rotation=dim.angle, size=dimtsz * 2, dxfattribs=attribs)
        else:
            scale = get_dxf_attr('dimasz')
            start = self.add_blockref(blk1, insert=start, scale=scale, rotation=dim.angle, reverse=True,
                                      dxfattribs=attribs)
            end = self.add_blockref(blk2, insert=end, scale=scale, rotation=dim.angle, dxfattribs=attribs)
        return start, end

    def get_text_midpoint(self, start: Vector, end: Vector) -> Vector:
        tad = self.dim_style.get('dimtad', 1)
        height = self.text_height
        gap = self.dim_style.get('dimgap', 0.625)
        dist = height / 2. + gap  # above dimline
        if tad == 0:  # center of dimline
            dist = 0
        elif tad == 4:  # below dimline
            dist = -dist
        base = end - start
        ortho = base.orthogonal().normalize(dist)
        return start.lerp(end) + ortho


class DimensionRenderer:
    def dispatch(self, dimension: 'Dimension', ucs: 'UCS', text_style: str = None) -> None:
        dwg = dimension.drawing
        dim_style = dimension.dim_style()
        block = dwg.blocks.new_anonymous_block(type_char='D')
        dimension.dxf.geometry = block.name
        dim_type = dimension.dim_type

        if dim_type in (0, 1):
            self.linear(dimension, dim_style, block, ucs, text_style)
        elif dim_type == 2:
            self.angular(dimension, dim_style, block, ucs, text_style)
        elif dim_type == 3:
            self.diameter(dimension, dim_style, block, ucs, text_style)
        elif dim_type == 4:
            self.radius(dimension, dim_style, block, ucs, text_style)
        elif dim_type == 5:
            self.angular3p(dimension, dim_style, block, ucs, text_style)
        elif dim_type == 6:
            self.ordinate(dimension, dim_style, block, ucs, text_style)
        else:
            raise DXFValueError("Unknown DIMENSION type: {}".format(dim_type))

    def linear(self, dimension: 'Dimension', dim_style: 'DimStyle', block: 'BlockLayout', ucs: 'UCS',
               text_style: str = None):
        """
        Call renderer for linear dimension lines: horizontal, vertical and rotated
        """
        render = LinearDimension(dimension, dim_style, block, ucs, text_style)
        render.render()

    def angular(self, dimension: 'Dimension', dim_style: 'DimStyle', block: 'BlockLayout', ucs: 'UCS',
                text_style: str = None):
        raise NotImplemented

    def diameter(self, dimension: 'Dimension', dim_style: 'DimStyle', block: 'BlockLayout', ucs: 'UCS',
                 text_style: str = None):
        raise NotImplemented

    def radius(self, dimension: 'Dimension', dim_style: 'DimStyle', block: 'BlockLayout', ucs: 'UCS',
               text_style: str = None):
        raise NotImplemented

    def angular3p(self, dimension: 'Dimension', dim_style: 'DimStyle', block: 'BlockLayout', ucs: 'UCS',
                  text_style: str = None):
        raise NotImplemented

    def ordinate(self, dimension: 'Dimension', dim_style: 'DimStyle', layout: 'BlockLayout', ucs: 'UCS',
                 text_style: str = None):
        raise NotImplemented


def format_text(value: float, dimrnd: float = None, dimdec: int = None, dimzin: int = 0, dimdsep: str = '.',
                dimpost: str = '<>', raisedec=False) -> str:
    if dimrnd is not None:
        value = xround(value, dimrnd)

    if dimdec is None:
        fmt = "{:f}"
        dimzin = dimzin | 8  # remove pending zeros for undefined decimal places, '{:f}'.format(0) -> '0.000000'
    else:
        fmt = "{:." + str(dimdec) + "f}"
    text = fmt.format(value)

    leading = bool(dimzin & 4)
    pending = bool(dimzin & 8)
    text = suppress_zeros(text, leading, pending)
    if raisedec:
        text = raise_decimals(text)
    if dimdsep != '.':
        text = text.replace('.', dimdsep)
    if dimpost:
        if '<>' in dimpost:
            fmt = dimpost.replace('<>', '{}', 1)
            text = fmt.format(text)
        else:
            raise DXFValueError('Invalid dimpost string: "{}"'.format(dimpost))
    return text