"""Builds small sheet-metal STEP files for the STEP-import tests (no SolidWorks around): an L bracket with a hole, and an assembly of two of them + a block."""
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Transform
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.IFSelect import IFSelect_RetDone
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDataStd import TDataStd_Name
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.TopLoc import TopLoc_Location


def _prism(pts, width, fillets):
    """Polygon in the XZ plane (list of (x, z)) extruded along Y; fillets = [((x, z), radius)] on the Y-parallel edges through those corners."""
    poly = BRepBuilderAPI_MakePolygon()
    for x, z in pts:
        poly.Add(gp_Pnt(x, 0, z))
    poly.Close()
    solid = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(), gp_Vec(0, width, 0)).Shape()
    fil = BRepFilletAPI_MakeFillet(solid)
    exp = TopExp_Explorer(solid, TopAbs_EDGE)
    while exp.More():
        e = TopoDS.Edge(exp.Current())
        p0, p1 = BRep_Tool.Pnt_s(_vertex(e, 0)), BRep_Tool.Pnt_s(_vertex(e, 1))
        if abs(p0.Y() - p1.Y()) > 1:
            for (x, z), r in fillets:
                if abs(p0.X() - x) < 1e-6 and abs(p0.Z() - z) < 1e-6:
                    fil.Add(r, e)
        exp.Next()
    return fil.Shape()


def _hole(solid, x, y, z, direction, d, length):
    cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*direction)), d / 2, length).Shape()
    return BRepAlgoAPI_Cut(solid, cyl).Shape()


def l_bracket(t=2.0, r=2.0, a=40.0, b=30.0, width=50.0, hole=6.0):
    """L profile (outside sizes a, b), extruded along Y by `width`; inner bend radius r, outer r + t; a hole in the flange of length a."""
    solid = _prism([(0, 0), (a, 0), (a, t), (t, t), (t, b), (0, b)], width, [((t, t), r), ((0, 0), r + t)])
    return _hole(solid, a - 12, width / 2, -1, (0, 0, 1), hole, t + 2) if hole else solid


def u_channel(t=2.0, r=2.0, w=60.0, h=30.0, width=80.0):
    """U profile (outside width w, leg height h) along Y; a 5 mm hole in the left leg, 20 from its end and 25 along the channel."""
    solid = _prism([(0, 0), (w, 0), (w, h), (w - t, h), (w - t, t), (t, t), (t, h), (0, h)], width, [((t, t), r), ((w - t, t), r), ((0, 0), r + t), ((w, 0), r + t)])
    return _hole(solid, -1, 25, h - 20, (1, 0, 0), 5.0, t + 2)


def _vertex(edge, i):
    from OCP.TopExp import TopExp
    return TopExp.FirstVertex_s(edge) if i == 0 else TopExp.LastVertex_s(edge)


def write_step(path, parts):
    """parts: [(name, shape, [(dx, dy, dz) per instance])] -> one assembly STEP with those components (instances share the part)."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString('XmlOcaf'))
    app.NewDocument(TCollection_ExtendedString('XmlOcaf'), doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    root = st.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString('ASM'))
    for name, shape, offsets in parts:
        lab = st.AddShape(shape, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(name))
        for dx, dy, dz in offsets:
            tr = gp_Trsf()
            tr.SetTranslation(gp_Vec(dx, dy, dz))
            st.AddComponent(root, lab, TopLoc_Location(tr))
    st.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.Transfer(doc)
    assert w.Write(path) == IFSelect_RetDone


if __name__ == '__main__':
    import os
    d = os.path.join(os.path.dirname(__file__), 'data')
    write_step(os.path.join(d, 'asm.step'), [('Конзола', l_bracket(), [(0, 0, 0), (100, 0, 0)]), ('Блок', BRepPrimAPI_MakeBox(30, 30, 30).Shape(), [(0, 100, 0)])])
    write_step(os.path.join(d, 'bracket.step'), [('Конзола', l_bracket(), [(0, 0, 0)])])
    print('written')
