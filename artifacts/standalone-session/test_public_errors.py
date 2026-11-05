"""Public refusal text must be authored here, never built from vendor data. Stdlib only."""
import ast
from pathlib import Path
import unittest
from public_errors import PUBLIC_CLASSES,GENERIC_MESSAGE,Refused,NotReady,Unavailable,Busy,OutcomeUnknown,CloudRejected,describe

ROOT=Path(__file__).resolve().parent
NAMES={cls.__name__ for cls in PUBLIC_CLASSES}


class LiteralMessageTests(unittest.TestCase):
    def test_every_public_raise_uses_one_string_literal(self):
        offenders=[]
        checked=0
        for path in sorted(ROOT.glob('*.py')):
            if path.name.startswith('test_'):continue
            tree=ast.parse(path.read_text(),filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node,ast.Call):continue
                name=getattr(node.func,'id',None) or getattr(node.func,'attr',None)
                if name not in NAMES:continue
                checked+=1
                literal=(len(node.args)==1 and not node.keywords and isinstance(node.args[0],ast.Constant)
                         and isinstance(node.args[0].value,str) and node.args[0].value.strip())
                if not literal:offenders.append(f'{path.name}:{node.lineno}')
        self.assertGreater(checked,20,'scanner found suspiciously few public raises')
        self.assertEqual(offenders,[],'public error messages must be single string literals')

    def test_no_em_or_en_dash_in_public_messages(self):
        for path in sorted(ROOT.glob('*.py')):
            if path.name.startswith('test_'):continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.Call) and (getattr(node.func,'id',None) in NAMES) and node.args:
                    value=getattr(node.args[0],'value','')
                    self.assertNotIn('—',value,f'{path.name}:{node.lineno}')
                    self.assertNotIn('–',value,f'{path.name}:{node.lineno}')


class DescribeTests(unittest.TestCase):
    def test_public_errors_keep_builtin_compatibility(self):
        for cls,base in [(Refused,ValueError),(NotReady,PermissionError),(Unavailable,ConnectionError),
                         (Busy,RuntimeError),(OutcomeUnknown,RuntimeError),(CloudRejected,RuntimeError)]:
            with self.subTest(cls=cls.__name__),self.assertRaises(base):raise cls('x')

    def test_only_public_text_is_echoed(self):
        self.assertEqual(describe(Refused('Stop the test first')),{'error':'Refused','code':'refused','message':'Stop the test first'})
        self.assertEqual(describe(OutcomeUnknown('Check the A2'))['code'],'outcome_unknown')
        for error in (ValueError("invalid literal for int() with base 10: 'secret-device-id'"),
                      KeyError('access_token'),RuntimeError('https://vendor.example/?token=abc')):
            with self.subTest(error=type(error).__name__):
                body=describe(error)
                self.assertEqual(body['message'],GENERIC_MESSAGE);self.assertEqual(body['code'],'failed')
                self.assertNotIn('secret',str(body));self.assertNotIn('token',str(body))


if __name__=='__main__':unittest.main()
