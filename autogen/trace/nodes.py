import copy

from dataclasses import dataclass
import warnings


from typing import Optional, List, Dict, Callable, Union, Type, Any
from autogen.agentchat.conversable_agent import ConversableAgent
from autogen.agentchat.agent import Agent
import copy
# from autogen.trace.nodes import Node
from collections import defaultdict

class Registry:
    """ A global registry of all the nodes. """

    def __init__(self):
        self._nodes = {} # a lookup table to find nodes by name
        self._levels = defaultdict(list)  # a lookup table to find nodes at a certain level # TODO should this be a list?

    def register(self, node):
        assert isinstance(node, Node)
        assert len(node.name.split(':'))==2
        if node.name in self._nodes:
            # increment the id
            name, id = node.name.split(':')
            node._name = name + ':' + str(int(id)+1)
        self._nodes[node.name] = node
        self._levels[node._level].append(node)

    def get(self, name):
        return self._nodes[name]

    def __str__(self):
        return str(self._nodes)

GRAPH = Registry()

class AbstractNode:
    """ An abstract data node in a directed graph (child --> paraent).
    """
    def __init__(self, value, *, name=None, trainable=False) -> None:
        self._parents = []
        self._children = []
        self._level = 0  # leaves are at level 0
        self._name = str(type(value).__name__)+':0' if name is None else  name+':0'
        if isinstance(value, Node):  # copy constructor
            self._data = copy.deepcopy(value._data)
            self._name = value._name
        else:
            self._data = value
        GRAPH.register(self)

    @property
    def data(self):
        return self._data

    @property
    def children(self):
        return self._children

    @property
    def parents(self):
        return self._parents

    @property
    def name(self):
        return self._name

    def add_parent(self, parent):
        assert isinstance(parent, Node), f"{parent} is not a Node."
        parent.add_child(self)

    def add_child(self, child):
        assert isinstance(child, Node), f"{child} is not a Node."
        assert self not in child.parents, f"{self} is already a parent of {child}."
        child._parents.append(self)
        assert child not in self.children, f"{child} is already a child of {self}."
        self._children.append(child)
        self._update_level(max(self._level, child._level+1))  # Update the level, because the child is added

    def _update_level(self, new_level):
        GRAPH._levels[self._level].remove(self)
        GRAPH._levels[new_level].append(self)
        assert all([ len(GRAPH._levels[i])>0 for i in range(len(GRAPH._levels)) ]), "Some levels are empty."

    def __str__(self) -> str:
        return f'Node: ({self.name}, dtype={type(self.data)})'

class Node(AbstractNode):
    """ Node for Autogen messages and prompts"""
    def __init__(self, value, *, name=None, trainable=False) -> None:
        # TODO only take in a dict with a certain structure
        if isinstance(value, str):
            warnings.warn("Initializing a Node with str is deprecated. Use dict instead.")
        assert  isinstance(value, str) or isinstance(value, dict) or isinstance(value, Node), f"Value {value} must be a string, a dict, or a Node."
        super().__init__(value, name=name)
        self.trainable = trainable
        self._feedback = None  # (analogous to gradient) this is the (synthetic) feedback from the user

    # We overload some magic methods to make it behave like a dict
    def __getattr__(self, name):
        if type(self._data) == dict:  # If attribute cannot be found, try to get it from the data
            return self._data.__getattribute__(name)
        else:
            raise AttributeError(f"{self} has no attribute {name}.")

    def __len__(self):
        return len(self._data)

    def __length_hint__(self):
        return NotImplemented

    def __getitem__(self, key):
        warnings.warn(f"Attempting to get {key} from {self.name}.")
        return self._data[key]

    def __setitem__(self, key, value):
        warnings.warn(f"Attemping to set {key} in {self.name}.")
        self._data[key] = value

    def __delitem__(self, key):
        del self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __reverse__(self):
        return reversed(self._data)

    def __contains__(self, key):
        return key in self._data

# TODO
class ParameterNode(Node):
    # This is a shorthand of a trainable Node.
    def __init__(self, value, *, name=None, trainable=True) -> None:
        super().__init__(value, name=name, trainable=trainable)



class MessageNode(Node):
    """ Output of an operator. """
    def __init__(self, value, mapping, *, args=None, kwargs=None, name=None) -> None:
        super().__init__(value, name=name)
        self._mapping = mapping
        self._args = () if args is None else args
        self._kwargs = {} if kwargs is None else kwargs
        for v in self._args:
            self.add_child(v)
        for v in self._kwargs.values():
            self.add_child(v)

    # def __getattr__(self, name):
    #     # If attribute cannot be found, try to get it from the data
    #     attr = self._data.__getattribute__(name)  # TODO
    #     # TODO add assertion
    #     if callable(attr):
    #         return trace(attr) # TODO
    #     else:
    #         output = Node(attr)
    #         output.register_mapping(f"{output.name}={self.name}.{name}", self)
    #         return attr
