"""Define the primary jobflow database interface."""

from __future__ import annotations

import typing

from maggma.core import Store
from monty.json import MSONable

if typing.TYPE_CHECKING:
    from enum import Enum
    from pathlib import Path
    from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, Union

    from maggma.core import Sort
    from maggma.stores import GridFSStore, MongoStore, MongoURIStore, S3Store

    save_type = Optional[
        Union[str, Enum, Type[MSONable], List[Union[Enum, str, Type[MSONable]]]]
    ]
    load_type = Union[bool, save_type]

T = typing.TypeVar("T", bound="JobStore")


class JobStore(Store):
    """
    Store intended to allow pushing and pulling documents into multiple stores.

    Parameters
    ----------
    docs_store
        Store for basic documents.
    data_store
        Maggma store for large data objects.
    save
        List of keys to save in the data store when uploading documents.
    load
        List of keys to load from the data store when querying.
    """

    def __init__(
        self,
        docs_store: Store,
        data_store: Store,
        save: save_type = None,
        load: load_type = False,
    ):
        self.docs_store = docs_store
        self.data_store = data_store

        # enforce uuid key
        self.data_store.key = "blob_id"
        self.docs_store.key = "uuid"

        if save is None or save is False:
            save = []
        elif not isinstance(save, (list, tuple)):
            save = [save]
        self.save = save

        if load is None:
            load = False
        if not isinstance(load, (list, tuple, bool)):
            load = [load]
        self.load = load

        kwargs = {
            k: getattr(docs_store, k)
            for k in ("key", "last_updated_field", "last_updated_type")
        }
        super().__init__(**kwargs)

    def name(self) -> str:
        """Get the name of the data source.

        Returns
        -------
        str
            A string representing this data source.
        """
        return f"JobStore-{self.docs_store.name}"

    def connect(self, force_reset: bool = False):
        """
        Connect to the source data.

        Parameters
        ----------
        force_reset
            Whether to reset the connection or not.
        """
        self.docs_store.connect(force_reset=force_reset)
        self.data_store.connect(force_reset=force_reset)

    def close(self):
        """Close any connections."""
        self.docs_store.close()
        self.data_store.close()

    def count(self, criteria: Optional[Dict] = None) -> int:
        """
        Count the number of documents matching the query criteria.

        Parameters
        ----------
        criteria
            PyMongo filter for documents to count in.

        Returns
        -------
        int
            The number of documents matching the query.
        """
        return self.docs_store.count(criteria=criteria)

    def query(
        self,
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Union[Sort, int]]] = None,
        skip: int = 0,
        limit: int = 0,
        load: load_type = None,
    ) -> Iterator[Dict]:
        """
        Query the JobStore for documents.

        Parameters
        ----------
        criteria
            PyMongo filter for documents to search in.
        properties
            Properties to return in grouped documents.
        sort
            Dictionary of sort order for fields. Keys are field names and values are 1
            for ascending or -1 for descending.
        skip
            Number of documents to skip.
        limit
            Limit on the total number of documents returned.
        load
            List of keys to load from the data store.

        Yields
        ------
        Dict
            The documents.
        """
        from pydash import get

        from jobflow.utils.find import find_key, update_in_dictionary

        if load is None:
            load = self.load

        load_keys = _prepare_load(load)

        if isinstance(properties, (list, tuple)):
            properties += ["uuid", "index"]
        elif isinstance(properties, dict):
            properties.update({"uuid": 1, "index": 1})

        docs = self.docs_store.query(
            criteria=criteria, properties=properties, sort=sort, skip=skip, limit=limit
        )

        for doc in docs:
            if load_keys:
                # Process is
                # 1. Find the locations of all blob identifiers.
                # 2. Filter the locations based on the load criteria.
                # 3. Resolve all data blobs using the data store.
                # 4. Insert the data blobs into the document
                locations = find_key(doc, "blob_uuid")
                blobs = [get(doc, list(loc)) for loc in locations]
                blobs, locations = _filter_blobs(blobs, locations, load_keys)

                object_info = {b["blob_uuid"]: loc for b, loc in zip(blobs, locations)}
                objects = self.data_store.query(
                    criteria={"blob_uuid": {"$in": list(object_info.keys())}},
                    properties=["blob_uuid", "data"],
                )
                object_map = {o["blob_uuid"]: o["data"] for o in objects}

                to_insert = {
                    tuple(loc): object_map[oid] for oid, loc in object_info.items()
                }
                update_in_dictionary(doc, to_insert)

            yield doc

    def query_one(
        self,
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Union[Sort, int]]] = None,
        load: load_type = None,
    ) -> Optional[Dict]:
        """
        Query the Store for a single document.

        Parameters
        ----------
        criteria
            PyMongo filter for documents to search.
        properties
            Properties to return in the document.
        sort
            Dictionary of sort order for fields. Keys are field names and values are 1
            for ascending or -1 for descending.
        load
            List of keys to load from the data store.

        Returns
        -------
        dict or None
            The document.
        """
        docs = self.query(
            criteria=criteria, properties=properties, load=load, sort=sort, limit=1
        )
        d = next(docs, None)
        return d

    def update(
        self,
        docs: Union[List[Dict], Dict],
        key: Union[List, str, None] = None,
        save: Union[bool, save_type] = None,
    ):
        """
        Update or insert documents into the Store.

        Parameters
        ----------
        docs
            The document or list of documents to update.
        key
            Field name(s) to determine uniqueness for a document, can be a list of
            multiple fields, a single field, or None if the Store's key field is to
            be used.
        save
            List of keys to save in the data store when uploading documents.
        """
        from enum import Enum

        from monty.json import jsanitize
        from pydash import get

        from jobflow.utils.find import find_key, update_in_dictionary

        save_keys = []
        if save is None or save is True:
            save_keys = self.save
        elif not isinstance(save, (tuple, list, bool)):
            save_keys = [save]
        elif isinstance(save, (tuple, list)):
            save_keys = save
        save_keys = [o.value if isinstance(o, Enum) else o for o in save_keys]

        if not isinstance(docs, list):
            docs = [docs]

        if key is not None:
            key = ["uuid", "index"]

        blob_data = []
        dict_docs = []
        for doc in docs:
            doc = jsanitize(doc, strict=True)
            dict_docs.append(doc)

            if save_keys:
                locations = []
                for save_key in save_keys:
                    locations.extend(find_key(doc, save_key, include_end=True))
                objects = [get(doc, list(loc)) for loc in locations]
                object_map = dict(zip(map(tuple, locations), objects))
                object_info = {k: _get_blob_info(o) for k, o in object_map.items()}
                update_in_dictionary(doc, object_info)

                # Now format blob data for saving in the data_store
                for loc, data in object_map.items():
                    blob = object_info[loc]
                    blob.update(
                        {
                            "data": data,
                            "job_uuid": doc["uuid"],
                            "job_index": doc["index"],
                        }
                    )
                    blob_data.append(blob)

        self.docs_store.update(dict_docs, key=key)

        if save_keys:
            self.data_store.update(blob_data, key="blob_uuid")

    def ensure_index(self, key: str, unique: bool = False) -> bool:
        """
        Try to create an index on document store and return True success.

        Parameters
        ----------
        key
            Single key to index.
        unique
            Whether or not this index contains only unique keys.

        Returns
        -------
        bool
            Whether the index exists/was created correctly.
        """
        return self.docs_store.ensure_index(key, unique=unique)

    def groupby(
        self,
        keys: Union[List[str], str],
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Union[Sort, int]]] = None,
        skip: int = 0,
        limit: int = 0,
        load: load_type = None,
    ) -> Iterator[Tuple[Dict, List[Dict]]]:
        """
        Group documents by keys.

        Parameters
        ----------
        keys
            Fields to group documents.
        criteria
            PyMongo filter for documents to search in.
        properties
            Properties to return in grouped documents
        sort
            Dictionary of sort order for fields. Keys are field names and values are 1
            for ascending or -1 for descending.
        skip
            Number of documents to skip.
        limit
            Limit on the total number of documents returned.
        load
            List of keys to load from the data store.

        Yields
        ------
        dict, list[dict]
            The documents as (key, documents) grouped by their keys.
        """
        from itertools import groupby

        from pydash import get, has, set_

        keys = keys if isinstance(keys, list) else [keys]

        if isinstance(properties, dict):
            # make sure all keys are in properties...
            properties.update(dict(zip(keys, [1] * len(keys))))

        docs = self.query(
            properties=properties,
            criteria=criteria,
            sort=sort,
            skip=skip,
            limit=limit,
            load=load,
        )
        data = [doc for doc in docs if all(has(doc, k) for k in keys)]

        def grouping_keys(doc):
            return tuple(get(doc, k) for k in keys)

        for vals, group in groupby(sorted(data, key=grouping_keys), key=grouping_keys):
            doc: Dict[str, Any] = {}
            for k, v in zip(keys, vals):
                set_(doc, k, get(v, k))
            yield doc, list(group)

    def remove_docs(self, criteria: Dict):
        """
        Remove docs matching the criteria.

        Parameters
        ----------
        criteria
            Criteria for documents to remove.
        """
        docs = self.query(criteria, properties=["uuid", "index"])
        for doc in docs:
            self.data_store.remove_docs(
                {"job_uuid": doc["uuid"], "job_index": doc["index"]}
            )
        self.docs_store.remove_docs(criteria)

    @property
    def collection(self):
        """Get the collection name."""
        return self.docs_store.collection

    @classmethod
    def from_store(cls, store: Store, **kwargs) -> JobStore:
        """
        Create a new job store that uses the same store for documents and data.

        Parameters
        ----------
        store
            A maggma store.
        **kwargs
            Keyword arguments that will be passed to the JobStore init method.

        Returns
        -------
        JobStore
            An :obj:`JobStore`.
        """
        from copy import deepcopy

        return cls(docs_store=store, data_store=deepcopy(store), **kwargs)

    def get_output(
        self,
        uuid: str,
        which: str = "last",
        load: load_type = False,
    ):
        """
        `Get the output from of a job UUID.

        Parameters
        ----------
        uuid
            A job UUID.
        which
            If there are multiple job runs, which index to use. Options are:
            - `"last"` (default): Use the last job that ran.
            - `"first"`: Use the first job that ran.
            - `"all"`: Return all outputs.
        load
            The keys to load from the datastore.

        Returns
        -------
        Any
            The output(s) for the job UUID.
        """
        if which in ("last", "first"):
            sort = -1 if which == "last" else 1

            result = self.query_one(
                {"uuid": uuid}, ["output"], {"index": sort}, load=load
            )

            if result is None:
                raise ValueError(f"{uuid} has not outputs.")

            return result["output"]
        else:
            results = list(
                self.query({"uuid": uuid}, ["output"], {"index": -1}, load=load)
            )

            if len(results) == 0:
                raise ValueError(f"{uuid} has not outputs.")

            return [r["output"] for r in results]

    @classmethod
    def from_db_file(
        cls: Type[T], db_file: Union[str, Path], admin: bool = True, **kwargs
    ) -> T:
        """
        Create an JobStore from a database file.

        Multiple options are supported for the database file. The file should be in
        json or yaml format.

        The simplest format is a monty dumped version of the store, generated using:

        >>> from monty.serialization import dumpfn
        >>> dumpfn("job_store.json", job_store)

        Alternatively, the format can be a dictionary containing the docs_store and
        data_store keys, with the values as the dictionary representation of those
        stores.

        Alternatively, a file format specific to mongodb is supported. Here the file
        should contain the keys:

        - host (str): The hostname of the database.
        - port (int): The port used to access the database.
        - collection (str): The collection in which to store documents.
        - authsource (str, optional): Authorization source for connecting to the
          database.
        - host_uri (str, optional): URI string specifying the database and login
          details. If this is specified, any other authentication information will be
          ignored.
        - admin_user (str, optional): The username for an account with admin privileges.
        - admin_password (str, optional): The password for an account with admin
          privileges.
        - readonly_user (str, optional): The username for an account with read
          privileges only.
        - readonly_password (str, optional): The password for an account with read
          privileges only.
        - data_store_kwargs (dict, optional): Keyword arguments that determine where
          to store large calculation data (band structures, density of states, charge
          density etc). Leaving this argument blank indicates that GridFS will be used.
          Alternatively, the following stores and keyword arguments are supported:
        - docs_store_kwargs: Additional keyword arguments that are passed to the
          MongoStore (and, optionally, GridFSStore if no data_store_kwargs are given).

          **S3 Store kwargs**

          - bucket (str): The S3 bucket where the data is stored.
          - s3_profile (str): The S3 profile that contains the login information
            typically found at ~/.aws/credentials.
          - compress (bool): Whether compression is used.
          - endpoint_url (str): The URL used to access the S3 store.

        - data_store_prefix (str, optional): The prefix for the collection used
          for the datastore.

        Parameters
        ----------
        db_file
            Path to the file containing the credentials.
        admin
            Whether to use the admin user (only applicable to the mongodb style file
            format).
        **kwargs
            Additional keyword arguments that get passed to the JobStore
            constructor.

        Returns
        -------
        JobStore
            An JobStore.
        """
        from monty.serialization import loadfn

        credentials = loadfn(db_file)

        if isinstance(credentials, JobStore):
            return credentials

        if "docs_store" in credentials and "data_store" in credentials:
            docs_store = credentials["docs_store"]
            data_store = credentials["data_store"]
        elif "docs_store" in credentials or "data_store" in credentials:
            raise ValueError(
                "Both or neither of docs_store and data_store must be specified."
            )
        else:
            docs_store = _get_docs_store(credentials, admin)
            data_store = _get_data_store(credentials, admin)

        return cls(docs_store, data_store, **kwargs)


def _get_docs_store(credentials: Dict[str, str], admin: bool) -> MongoStore:
    """
    Get the docs store from mongodb credentials.

    See JobStore.from_db_file for supported mongo connection arguments.
    """
    return _get_mongo_like_store(credentials["collection"], credentials, admin)


def _get_data_store(credentials: Dict[str, Any], admin: bool) -> Store:
    """
    Get the data store from database credentials.

    See JobStore.from_db_file for supported store types and connection arguments.
    """
    data_store_prefix = credentials.get("data_store_prefix", "job")
    collection_name = f"{data_store_prefix}_datastore"

    if "data_store_kwargs" not in credentials:
        # Use GridFS to store data
        if "host_uri" in credentials:
            raise ValueError("GridFS from URI specification not supported.")
        else:
            auth = _get_mongo_auth(credentials, admin)
            auth.update(credentials.get("mongo_store_kwargs", {}))
            return GridFSStore(
                credentials["database"], collection_name, compression=True, **auth
            )

    data_store_kwargs = credentials.get("data_store_kwargs", {})
    if "bucket" in data_store_kwargs:
        # Store is a S3 bucket
        index_collection_name = f"{collection_name}_index"
        index_store = _get_mongo_like_store(index_collection_name, credentials, admin)
        index_store.key = "fs_id"

        return S3Store(
            index=index_store, sub_dir=collection_name, key="fs_id", **data_store_kwargs
        )
    raise ValueError("Unsupported data store")


def _get_mongo_like_store(
    collection_name: str, credentials: Dict[str, Any], admin: bool
) -> Union[MongoStore, MongoURIStore]:
    """Get either a MongoStore or MongoURIStore from a collection and credentials."""
    mongo_store_kwargs = credentials.get("mongo_store_kwargs", {})

    if "host_uri" in credentials:
        return MongoURIStore(
            credentials["host_uri"],
            credentials.get("database", None),
            collection_name,
            **mongo_store_kwargs,
        )

    auth = _get_mongo_auth(credentials, admin)
    auth.update(mongo_store_kwargs)
    return MongoStore(credentials["database"], collection_name, **auth)


def _get_mongo_auth(credentials: Dict[str, Any], admin: bool) -> Dict[str, Any]:
    """Get mongo authentication kwargs from the credentials specification."""
    auth = {}
    if admin and "admin_user" not in credentials:
        raise ValueError(
            "Trying to use admin credentials, but no admin credentials are defined. "
            "Use admin=False if only read_only credentials are available."
        )

    if admin:
        auth["user"] = credentials.get("admin_user", "")
        auth["password"] = credentials.get("admin_password", "")
    else:
        auth["user"] = credentials.get("readonly_user", "")
        auth["password"] = credentials.get("readonly_password", "")

    # this way, we won't override the MongoStore defaults
    for key in ("host", "port"):
        if key in credentials:
            auth[key] = credentials["key"]

    auth["authsource"] = credentials.get("authsource", credentials["database"])
    return auth


def _prepare_load(load: load_type) -> Union[bool, List[Union[str, Tuple[str, str]]]]:
    """Standardize load types."""
    from enum import Enum

    if load is None:
        return []

    if isinstance(load, bool):
        return load

    if not isinstance(load, (tuple, list)):
        load = [load]

    new_load = []
    for ltype in load:
        if isinstance(ltype, Enum):
            new_load.append(ltype.value)
        elif not isinstance(ltype, str) and issubclass(ltype, MSONable):
            new_load.append((ltype.__module__, ltype.__name__))
        else:
            new_load.append(ltype)
    return new_load


def _filter_blobs(
    blob_infos: List[Dict],
    locations: List[List[Any]],
    load: Union[bool, List[Union[str, Tuple[str, str]]]] = None,
) -> Tuple[List[Dict], List[List[Any]]]:
    if load is True:
        # return all blobs
        return blob_infos, locations

    if not load or isinstance(load, bool):
        # Don't return any blobs
        return [], []

    new_blobs = []
    new_locations = []
    for blob, location in zip(blob_infos, locations):
        for ltype in load:
            if (
                isinstance(ltype, tuple)
                and blob.get("@class", None) == ltype[1]
                and blob.get("@module", None) == ltype[0]
            ):
                pass
            elif location[-1] == ltype:
                pass
            else:
                continue

            new_blobs.append(blob)
            new_locations.append(location)

    return new_blobs, new_locations


def _get_blob_info(obj: Any) -> Dict[str, str]:
    from jobflow.utils.uuid import suuid

    class_name = ""
    module_name = ""
    if isinstance(obj, MSONable):
        class_name = obj.__class__.__name__
        module_name = obj.__class__.__module__

    return {"@class": class_name, "@module": module_name, "blob_uuid": suuid()}