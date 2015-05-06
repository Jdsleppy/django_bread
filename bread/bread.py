from six import string_types as six_string_types
from six.moves.http_client import BAD_REQUEST
from six.moves.urllib.parse import urlencode

from django.conf import settings
from django.conf.urls import url
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.models import Permission
from django.contrib.auth.views import redirect_to_login
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.urlresolvers import reverse_lazy
from django.db.models import Model
from django.forms.models import modelform_factory
from vanilla import ListView, DetailView, CreateView, UpdateView, DeleteView

from .utils import validate_fieldspec, get_verbose_name


"""
About settings.

You can provide a Django setting named BREAD as a dictionary.
Here are the settings, all currently optional:

DEFAULT_BASE_TEMPLATE: Default value for Bread's base_template argument

DEFAULT_TEMPLATE_NAME_PATTERN: Default value for Bread's
template_name_pattern argument.
"""


# Helper to get settings from BREAD dictionary, or default
def setting(name, default=None):
    BREAD = getattr(settings, 'BREAD', {})
    return BREAD.get(name, default)


class BreadViewMixin(object):
    """We mix this into all the views for some common features"""
    bread = None  # The Bread object using this view

    exclude = None
    form_class = None

    # Make this view require the appropriate permission
    @property
    def permission_required(self):
        return self.get_full_perm_name(self.perm_name)

    # Given a short permission name like 'change' or 'add', return
    # the full name like 'app_label.change_model' for this view's model.
    def get_full_perm_name(self, short_name):
        return "{app_name}.{perm_name}_{model_name}".format(
            app_name=self.bread.model._meta.app_label,
            model_name=self.bread.model._meta.object_name.lower(),
            perm_name=short_name,
        )

    def __init__(self, *args, **kwargs):
        # Make sure the permission needed to use this view exists.
        super(BreadViewMixin, self).__init__(*args, **kwargs)
        perm_name = '%s_%s' % (self.perm_name, self.bread.model._meta.object_name.lower())
        perm = Permission.objects.filter(
            content_type=ContentType.objects.get_for_model(self.bread.model),
            codename=perm_name
        ).first()
        if not perm:
            raise ImproperlyConfigured(
                "The view %r requires permission %s but there's no such permission"
                % (self, perm_name)
            )

    # Override dispatch to get our own custom version of the braces
    # PermissionRequired mixin.  Here's how ours behaves:
    #
    # If user is not logged in, redirect to a login page.
    # Else, if user does not have the required permission, return 403.
    # Else, carry on.
    def dispatch(self, request, *args, **kwargs):
        # Make sure that the permission_required attribute is set on the
        # view, or raise a configuration error.
        if self.permission_required is None:   # pragma: no cover
            raise ImproperlyConfigured(
                "'BreadViewMixin' requires "
                "'permission_required' attribute to be set.")

        # Check if the user is logged in
        if not request.user.is_authenticated():
            return redirect_to_login(request.get_full_path(),
                                     settings.LOGIN_URL,
                                     REDIRECT_FIELD_NAME)

        # Check to see if the request's user has the required permission.
        has_permission = request.user.has_perm(self.permission_required)

        if not has_permission:  # If the user lacks the permission
            raise PermissionDenied  # return a forbidden response.

        return super(BreadViewMixin, self).dispatch(request, *args, **kwargs)

    def get_template_names(self):
        # Is there a template_name_pattern?
        if self.bread.template_name_pattern:
            return [self.bread.template_name_pattern.format(
                app_label=self.bread.model._meta.app_label,
                model=self.bread.model._meta.object_name.lower(),
                view=self.template_name_suffix
            )]
        # First try the default names for Django Vanilla views, then
        # add on 'bread/<viewname>.html' as a final possibility.
        return (super(BreadViewMixin, self).get_template_names()
                + ['bread/%s.html' % self.template_name_suffix])

    def _get_new_url(self, **query_parms):
        """Return a new URL consisting of this request's URL, with any specified
        query parms updated or added"""
        request_kwargs = dict(self.request.GET)
        request_kwargs.update(query_parms)
        return self.request.path + "?" + urlencode(request_kwargs, doseq=True)

    def get_context_data(self, **kwargs):
        data = super(BreadViewMixin, self).get_context_data(**kwargs)
        # Include reference to the Bread object in template contexts
        data['bread'] = self.bread

        # Provide references to useful Model Meta attributes
        data['verbose_name'] = self.model._meta.verbose_name
        data['verbose_name_plural'] = self.model._meta.verbose_name_plural

        # Template that the default bread templates should extend
        data['base_template'] = self.bread.base_template

        # Add 'may_<viewname>' to the context for each view, so the templates can
        # tell if the current user may use the named view.
        data['may_browse'] = 'B' in self.bread.views \
                             and self.request.user.has_perm(self.get_full_perm_name('browse'))
        data['may_read'] = 'R' in self.bread.views \
                           and self.request.user.has_perm(self.get_full_perm_name('read'))
        data['may_edit'] = 'E' in self.bread.views \
                           and self.request.user.has_perm(self.get_full_perm_name('change'))
        data['may_add'] = 'A' in self.bread.views \
                          and self.request.user.has_perm(self.get_full_perm_name('add'))
        data['may_delete'] = 'D' in self.bread.views \
                             and self.request.user.has_perm(self.get_full_perm_name('delete'))
        return data

    def get_form(self, data=None, files=None, **kwargs):
        form_class = self.form_class or self.bread.form_class
        if not form_class:
            form_class = modelform_factory(
                self.bread.model,
                fields='__all__',
                exclude=self.exclude or self.bread.exclude
            )
        return form_class(data=data, files=files, **kwargs)

    @property
    def success_url(self):
        return reverse_lazy(self.bread.browse_url_name())


# The individual view classes we'll use and customize in the
# omnibus class below:
class BrowseView(BreadViewMixin, ListView):
    # Configurable:
    columns = []
    filterset = None  # Class
    paginate_by = None
    perm_name = 'browse'  # Not a default Django permission
    template_name_suffix = 'browse'

    def __init__(self, *args, **kwargs):
        super(BrowseView, self).__init__(*args, **kwargs)
        # Internal use
        self.filter = None

    def get_queryset(self):
        qset = super(BrowseView, self).get_queryset()

        # Now filter
        if self.filterset is not None:
            self.filter = self.filterset(self.request.GET, queryset=qset)
            qset = self.filter.qs
        return qset

    def get_context_data(self, **kwargs):
        data = super(BrowseView, self).get_context_data(**kwargs)
        data['columns'] = self.columns
        data['filter'] = self.filter
        if data.get('is_paginated', False):
            page = data['page_obj']
            num_pages = data['paginator'].num_pages
            if page.has_next():
                if page.next_page_number() != num_pages:
                    data['next_url'] = self._get_new_url(page=page.next_page_number())
                data['last_url'] = self._get_new_url(page=num_pages)
            if page.has_previous():
                data['first_url'] = self._get_new_url(page=1)
                if page.previous_page_number() != 1:
                    data['previous_url'] = self._get_new_url(page=page.previous_page_number())
        return data


class ReadView(BreadViewMixin, DetailView):
    """
    The read view makes a form, not because we're going to submit
    changes, but just as a handy container for the object's data that
    we can iterate over in the template to display it if we don't want
    to make a custom template for this model.
    """
    perm_name = 'read'  # Not a default Django permission
    template_name_suffix = 'read'

    def get_context_data(self, **kwargs):
        data = super(ReadView, self).get_context_data(**kwargs)
        data['form'] = self.get_form(instance=self.object)
        return data


class LabelValueReadView(ReadView):
    """A alternative read view that displays data from (label, value) pairs rather than a form.

    The (label, value) pairs are derived from a class attribute called fields. The tuples in
    fields are manipulated according to the rules below before being passed as simple strings
    to the template.

    Unlike ReadView, you must subclass LabelValueReadView to make it useful. In most cases, your
    subclass only needs to populate the fields attribute.

    Specifically, fields should be an iterable of 2-tuples of (label, evaluator).

    The label should be a string, or None. If it's None, the evaluator must be a Django model
    field. The label is created from the field's verbose_name attribute.

    The evaluator is evaluated in one of the following 5 modes, in this order --
      1) a string that matches an attribute on self.object. Resolves to the value of the attribute.
      2) a string that matches a method name on self.object. Resolves to the value of the method
      call.
      3) a string that's neither of the above. Resolves to itself.
      4) a non-instance function that accepts the context data dict as a parameter. Resolves to the
      value of the function. (Note that self.object is available to the called function via
      context_data['object'].)
      5) None of the above. Resolves to str(evaluator).

    Some examples:
    fields = ((None, 'id'),                         # Mode 1: self.object.id
              (_('The length'), '__len__'),         # Mode 2: self.object.__len__()
              (_('Foo'), 'bar'),                    # Mode 3: 'bar'
              (_('Stuff'), 'frob_all_the_things'),  # Mode 4: frob_all_the_things(context_data)
              (_('Answer'), 42),                    # Mode 5: '42'
              )
    """
    template_name_suffix = 'label_value_read'
    fields = []

    def get_context_data(self, **kwargs):
        context_data = super(LabelValueReadView, self).get_context_data(**kwargs)

        context_data['read_fields'] = [self.get_field_label_value(label, value, context_data) for
                                       label, value in self.fields]

        return context_data

    def get_field_label_value(self, label, evaluator, context_data):
        """Given a 2-tuple from fields, return the corresponding (label, value) tuple.

        Implements the modes described in the class docstring. (q.v.)
        """
        value = ''
        if isinstance(evaluator, six_string_types):
            if hasattr(self.object, evaluator):
                # This is an instance attr or method
                attr = getattr(self.object, evaluator)
                # Modes #1 and #2.
                value = attr() if callable(attr) else attr
                if label is None:
                    # evaluator refers to a model field (we hope).
                    label = get_verbose_name(self.object, evaluator)
            else:
                # It's a simple string (Mode #3)
                value = evaluator
        else:
            if callable(evaluator):
                # This is a non-instance method (Mode #4)
                value = evaluator(context_data)
            else:
                # Mode #5
                value = str(evaluator)
        return label, value


class EditView(BreadViewMixin, UpdateView):
    perm_name = 'change'  # Default Django permission
    template_name_suffix = 'edit'

    def form_invalid(self, form):
        # Return a 400 if the form isn't valid
        rsp = super(EditView, self).form_invalid(form)
        rsp.status_code = BAD_REQUEST
        return rsp


class AddView(BreadViewMixin, CreateView):
    perm_name = 'add'  # Default Django permission
    template_name_suffix = 'edit'  # Yes 'edit' not 'add'

    def form_invalid(self, form):
        # Return a 400 if the form isn't valid
        rsp = super(AddView, self).form_invalid(form)
        rsp.status_code = BAD_REQUEST
        return rsp


class DeleteView(BreadViewMixin, DeleteView):
    perm_name = 'delete'  # Default Django permission
    template_name_suffix = 'delete'


class Bread(object):
    """
    Provide a set of BREAD views for a model.

    Example usage:

        bread_views_for_model = Bread(Model, other kwargs...)
        ...
        urlpatterns += bread_views_for_model.get_urls()

    See `get_urls` for the resulting URL names and paths.

    It is expected that you subclass `Bread` and customize it by at least
    setting attributes on the subclass.

    Below, <name> refers to the lowercased model name, e.g. 'model'.

    Each view requires a permission. The expected permissions are named:

    * browse_<name>   (not a default Django permission)
    * read_<name>   (not a default Django permission)
    * change_<name>    (this is a default Django permission, used on the Edit view)
    * add_<name>    (this is a default Django permission)
    * delete_<name>    (this is a default Django permission)

    Parameters:

    Assumes templates with the following names:

        Browse - <app>/<name>_browse.html
        Read   - <app>/<name>_read.html
        Edit   - <app>/<name>_edit.html
        Add    - <app>/<name>_add.html
        Delete - <app>/<name>_confirm_delete.html

    but defaults to bread/<activity>.html if those aren't found.  The bread/<activity>.html
    templates are very generic, but you can pass 'base_template' as the name of a template
    that they should extend. They will supply `{% block title %}` and `{% block content %}`.

    OR, you can pass in template_name_pattern as a string that will be used to come up with
    a template name by substituting `{app_label}`, `{model}` (lower-cased model name), and
    `{view}` (`browse`, `read`, etc.).

    """
    browse_view = BrowseView
    read_view = ReadView
    edit_view = EditView
    add_view = AddView
    delete_view = DeleteView

    exclude = []  # Names of fields not to show
    views = "BREAD"
    base_template = setting('DEFAULT_BASE_TEMPLATE', 'base.html')
    namespace = ''
    template_name_pattern = setting('DEFAULT_TEMPLATE_NAME_PATTERN', None)
    plural_name = None
    form_class = None

    def __init__(self):
        self.name = self.model._meta.object_name.lower()
        self.views = self.views.upper()

        if not self.plural_name:
            self.plural_name = self.name + 's'

        if not issubclass(self.model, Model):
            raise TypeError("'model' argument for Bread must be a "
                            "subclass of Model; it is %r" % self.model)

        if self.browse_view.columns:
            for title, column in self.browse_view.columns:
                validate_fieldspec(self.model, column)

        if hasattr(self, 'paginate_by') or hasattr(self, 'columns'):
            raise ValueError("The 'paginate_by' and 'columns' settings have been moved "
                             "from the Bread class to the BrowseView class.")
        if hasattr(self, 'filter'):
            raise ValueError("The 'filter' setting has been renamed to 'filterset' and moved "
                             "to the BrowseView.")
        if hasattr(self, 'filterset'):
            raise ValueError("The 'filterset' setting should be on the BrowseView, not "
                             "the Bread view.")

    #####
    # B #
    #####
    def browse_url_name(self, include_namespace=True):
        """Return the URL name for browsing this model"""
        return self.get_url_name('browse', include_namespace)

    def get_browse_view(self):
        """Return a view method for browsing."""

        return self.browse_view.as_view(
            bread=self,
            model=self.model,
        )

    #####
    # R #
    #####
    def read_url_name(self, include_namespace=True):
        return self.get_url_name('read', include_namespace)

    def get_read_view(self):
        return self.read_view.as_view(
            bread=self,
            model=self.model,
            form_class=self.form_class,
        )

    #####
    # E #
    #####
    def edit_url_name(self, include_namespace=True):
        return self.get_url_name('edit', include_namespace)

    def get_edit_view(self):
        return self.edit_view.as_view(
            bread=self,
            model=self.model,
            form_class=self.form_class,
        )

    #####
    # A #
    #####
    def add_url_name(self, include_namespace=True):
        return self.get_url_name('add', include_namespace)

    def get_add_view(self):
        return self.add_view.as_view(
            bread=self,
            model=self.model,
            form_class=self.form_class,
        )

    #####
    # D #
    #####
    def delete_url_name(self, include_namespace=True):
        return self.get_url_name('delete', include_namespace)

    def get_delete_view(self):
        return self.delete_view.as_view(
            bread=self,
            model=self.model,
        )

    ##########
    # Common #
    ##########
    def get_url_name(self, view_name, include_namespace=True):
        if include_namespace:
            url_namespace = self.namespace + ':' if self.namespace else ''
        else:
            url_namespace = ''
        if view_name == 'browse':
            return '%s%s_%s' % (url_namespace, view_name, self.plural_name)
        else:
            return '%s%s_%s' % (url_namespace, view_name, self.name)

    def get_urls(self, prefix=True):
        """
        Return urlpatterns to add for this model's BREAD interface.

        By default, these will be of the form:

           Operation    Name                   URL
           ---------    --------------------   --------------------------
           Browse       browse_<plural_name>   <plural_name>/
           Read         read_<name>            <plural_name>/<pk>/
           Edit         edit_<name>            <plural_name>/<pk>/edit/
           Add          add_<name>             <plural_name>/add/
           Delete       delete_<name>          <plural_name>/<pk>/delete/

        Example usage:

            urlpatterns += my_bread.get_urls()

        If a restricted set of views is passed in the 'views' parameter, then
        only URLs for those views will be included.

        If prefix is False, ``<plural_name>/`` will not be included on
        the front of the URLs.

        """

        prefix = '%s/' % self.plural_name if prefix else ''

        urlpatterns = []
        if 'B' in self.views:
            urlpatterns.append(
                url(r'^%s$' % prefix,
                    self.get_browse_view(),
                    name=self.browse_url_name(include_namespace=False)))

        if 'R' in self.views:
            urlpatterns.append(
                url(r'^%s(?P<pk>\d+)/$' % prefix,
                    self.get_read_view(),
                    name=self.read_url_name(include_namespace=False)))

        if 'E' in self.views:
            urlpatterns.append(
                url(r'^%s(?P<pk>\d+)/edit/$' % prefix,
                    self.get_edit_view(),
                    name=self.edit_url_name(include_namespace=False)))

        if 'A' in self.views:
            urlpatterns.append(
                url(r'^%sadd/$' % prefix,
                    self.get_add_view(),
                    name=self.add_url_name(include_namespace=False)))

        if 'D' in self.views:
            urlpatterns.append(
                url(r'^%s(?P<pk>\d+)/delete/$' % prefix,
                    self.get_delete_view(),
                    name=self.delete_url_name(include_namespace=False)))
        return urlpatterns
